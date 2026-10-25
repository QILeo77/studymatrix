# agents/executor.py
# A1 交付 2：Tool Executor
# - 白名单校验：未注册工具直接拒绝
# - 参数类型校验（轻量 JSON Schema：type/required/min/max）
# - user 数据（profile）从调用上下文注入，禁止模型传入
# - 超时控制、异常捕获、结构化返回
# - 审计 trace：trace_id / step / tool / args / result 摘要 / 耗时

import json
import time
import uuid
from pathlib import Path

from agents.storage import execute, now_str
from agents.tools_registry import TOOL_REGISTRY

BASE_DIR = Path(__file__).resolve().parent.parent
TRACE_DIR = BASE_DIR / "data" / "agent_traces"
TRACE_DIR.mkdir(parents=True, exist_ok=True)

TOOL_TIMEOUT_SECONDS = 15   # 本地工具均为毫秒级，15s 足够宽松


def _validate_args(schema: dict, args: dict) -> str | None:
    """轻量 JSON Schema 校验：类型 + required + min/max，返回错误信息或 None"""
    if not isinstance(args, dict):
        return "工具参数必须是 JSON 对象"
    for req in schema.get("required", []):
        if req not in args:
            return f"缺少必填参数: {req}"
    props = schema.get("properties", {})
    for k, v in args.items():
        if k not in props:
            return f"未知参数: {k}（不在 Schema 中）"
        t = props[k].get("type")
        if t == "integer" and not isinstance(v, int):
            return f"参数 {k} 必须是整数"
        if t == "string" and not isinstance(v, str):
            return f"参数 {k} 必须是字符串"
        if t == "number" and not isinstance(v, (int, float)):
            return f"参数 {k} 必须是数字"
        if "minimum" in props[k] and isinstance(v, (int, float)) and v < props[k]["minimum"]:
            return f"参数 {k} 不得小于 {props[k]['minimum']}"
        if "maximum" in props[k] and isinstance(v, (int, float)) and v > props[k]["maximum"]:
            return f"参数 {k} 不得大于 {props[k]['maximum']}"
    return None


class ToolExecutor:
    """工具执行器：白名单 + 参数校验 + 超时 + 审计"""

    def __init__(self, user_context: dict = None):
        # user_context 由调用方（服务端）注入，模型侧不可见也不可传
        self.user_context = user_context or {}
        self.trace_id = "tr_" + uuid.uuid4().hex[:12]
        self.events = []   # 审计事件序列

    def execute(self, tool_name: str, args: dict, step_index: int) -> dict:
        """执行一个工具调用，返回结构化结果
        返回: {"success": bool, "error": str?, "retryable": bool, "result": dict?}
        """
        started = time.time()

        # 1. 白名单校验
        if tool_name not in TOOL_REGISTRY:
            self._audit(step_index, tool_name, args, {"rejected": "not_in_whitelist"}, time.time() - started)
            return {
                "success": False,
                "error": f"工具 {tool_name} 未注册（白名单外调用被拦截）",
                "retryable": False,
            }

        spec = TOOL_REGISTRY[tool_name]

        # 2. 参数 Schema 校验：失败返回结构化错误让模型重试
        err = _validate_args(spec["parameters"], args or {})
        if err:
            self._audit(step_index, tool_name, args, {"rejected": err}, time.time() - started)
            return {"success": False, "error": f"参数校验失败: {err}", "retryable": True}

        # 3. user 数据注入（需要 profile 的工具由上下文提供，模型传不进来）
        handler = spec["handler"]
        try:
            # 简易超时：本地工具同步执行，用时间片保护
            t0 = time.time()
            if spec.get("needs_context"):
                # 需要完整上下文（如 generate_plan：profile+scheduled+message）
                result = handler(args or {}, context=self.user_context)
            elif spec.get("needs_profile"):
                result = handler(args or {}, profile=self.user_context.get("profile"))
            elif spec.get("needs_user_schedule"):
                result = handler(args or {}, user_schedule=self.user_context.get("user_schedule"))
            else:
                result = handler(args or {})
            elapsed = time.time() - t0
            if elapsed > TOOL_TIMEOUT_SECONDS:
                raise TimeoutError(f"工具执行超过 {TOOL_TIMEOUT_SECONDS}s")

            self._audit(step_index, tool_name, args, result, time.time() - started)
            # 成功：合并 result（保持 {"success": True, ...} 结构）
            out = dict(result)
            out["success"] = True
            return out
        except Exception as e:
            self._audit(step_index, tool_name, args, {"exception": str(e)}, time.time() - started)
            return {"success": False, "error": f"工具执行异常: {e}", "retryable": True}

    # ---------------- 审计 ----------------

    def _audit(self, step_index, tool, args, result, elapsed):
        """单次调用审计（内存 + 落盘由 save_trace 统一完成）"""
        self.events.append({
            "step_index": step_index,
            "tool": tool,
            "args": args,
            "result_summary": _summarize(result),
            "elapsed_ms": round(elapsed * 1000, 1),
        })

    def save_trace(self, record: dict):
        """整轮 Agent 运行的 trace 落盘（可回放答辩用）+ 写入 SQLite"""
        trace = {"trace_id": self.trace_id, **record, "events": self.events}
        path = TRACE_DIR / f"{self.trace_id}.json"
        try:
            path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            print(f"[AgentTrace] trace 保存失败: {e}")
        # ★ 同时写入 SQLite（agent_traces 表），便于查询/统计
        try:
            execute(
                "INSERT OR REPLACE INTO agent_traces(trace_id, user_id, payload, created_at) "
                "VALUES (?,?,?,?)",
                (self.trace_id, record.get("user_id") or "",
                 json.dumps(trace, ensure_ascii=False), now_str()),
            )
        except Exception as e:
            print(f"[AgentTrace] DB 写入失败: {e}")
        return self.trace_id


def _summarize(result, limit=300) -> str:
    """结果摘要（审计只存摘要，不存全量）"""
    try:
        s = json.dumps(result, ensure_ascii=False)
        return s[:limit] + ("..." if len(s) > limit else "")
    except Exception:
        return str(result)[:limit]
