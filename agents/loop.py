# agents/loop.py
# A1 交付 3：Agent Loop（run_agent，生成器形态，逐步 yield 供 SSE 实时推送）
# - 控制权转移：工具调用顺序由大模型决定（DeepSeek Function Calling）
# - MAX_STEPS = 8；连续两次工具失败 → fallback 规则引擎（复用原 make_plan）
# - 每步产出事件：{"phase": "think"|"tool"|"final"|"fallback", ...}
# - 全程写 agent_trace（executor.save_trace）

import os

from dotenv import load_dotenv
from openai import OpenAI

from agents.executor import ToolExecutor
from agents.prompts import build_agent_system_prompt
from agents.tools_registry import get_tool_schemas

load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com",
)

MAX_STEPS = 8                      # Agent Loop 步数上限
CONSECUTIVE_FAIL_LIMIT = 2         # 连续工具失败降级阈值


def run_agent(user_message: str, profile: dict = None, schedule_text: str = "",
              extra_prompt: str = "", user_schedule=None, progress_data: str = "",
              user_id: str = "", completion_rate=None,
              completion_done=None, completion_total=None):
    """Agent 主循环（生成器）：逐步 yield 事件，最终返回 final 文本

    Args:
        extra_prompt: 追加到系统提示词末尾的强约束块（如精准定位/进度感知）
        user_schedule: 用户动态课表行 [{day,start,end,course}]（工具上下文注入）
        progress_data: 打卡进度文本（工具上下文注入，供 generate_plan 参考）
        user_id: 用户标识（写入 agent_traces 表用）
        completion_rate / completion_done / completion_total: 本周计划完成率（督学反馈）

    yield 事件格式:
        {"phase": "think",  "step": n, "content": "模型思考摘要"}
        {"phase": "tool",   "step": n, "tool": name, "args": {...}, "result": {...}}
        {"phase": "final",  "content": "最终 Markdown 回复", "trace_id": "..."}
        {"phase": "fallback", "reason": "..."}   # 降级到规则引擎
    """
    executor = ToolExecutor(user_context={
        "profile": profile or {},
        "user_schedule": user_schedule if user_schedule is not None else [],
        "user_message": user_message,     # generate_plan 需要「结合用户输入」
        "schedule_text": schedule_text,
        "progress_data": progress_data,
        "user_id": user_id or "default",
        # 督学反馈：本周计划完成率统计（供 generate_plan 注入约束）
        "completion_rate": completion_rate,
        "completion_done": completion_done,
        "completion_total": completion_total,
    })
    system_prompt = build_agent_system_prompt(profile or {}, schedule_text=schedule_text)
    if extra_prompt:
        system_prompt += "\n\n" + extra_prompt

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]
    tools = get_tool_schemas()

    consecutive_fails = 0
    final_answer = None
    steps = []   # trace 用：每步记录

    # ---------- Agent Loop ----------
    for step in range(1, MAX_STEPS + 1):
        try:
            response = _client.chat.completions.create(
                model="deepseek-chat",
                messages=messages,
                tools=tools,
                stream=False,
            )
        except Exception as e:
            # 模型调用失败：直接降级
            yield {"phase": "fallback", "reason": f"模型调用失败: {e}"}
            return

        msg = response.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None)

        # ---- 分支 1：模型决定调用工具（顺序由模型自主决定）----
        if tool_calls:
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            })
            # 思考摘要（若有）
            if msg.content:
                yield {"phase": "think", "step": step, "content": msg.content[:200]}
                steps.append({"step": step, "type": "think", "content": msg.content[:200]})

            for tc in tool_calls:
                tool_name = tc.function.name
                try:
                    args = json_loads(tc.function.arguments)
                except Exception:
                    args = {}

                # Tool Executor：白名单 + 校验 + 审计
                result = executor.execute(tool_name, args, step)
                yield {
                    "phase": "tool",
                    "step": step,
                    "tool": tool_name,
                    "args": args,
                    "result": result,
                }
                steps.append({"step": step, "type": "tool", "tool": tool_name,
                              "ok": result.get("success", False)})

                if result.get("success"):
                    consecutive_fails = 0
                else:
                    consecutive_fails += 1

                # 工具结果回填给模型（结构化）
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json_dumps(result),
                })

            # 连续失败降级
            if consecutive_fails >= CONSECUTIVE_FAIL_LIMIT:
                yield {"phase": "fallback",
                       "reason": f"连续 {consecutive_fails} 次工具失败，降级规则引擎"}
                return
            continue

        # ---- 分支 2：final（模型不再调工具）----
        final_answer = msg.content or ""
        break

    # ---------- 收尾 ----------
    if final_answer is None:
        # 超步数仍未 final → 降级
        yield {"phase": "fallback", "reason": f"超过 MAX_STEPS={MAX_STEPS}，降级规则引擎"}
        return

    trace_id = executor.save_trace({
        "user_id": user_id or "default",
        "user_message": user_message,
        "profile": profile or {},
        "steps": steps,
        "final_output": final_answer[:2000],
        "model": "deepseek-chat",
        "max_steps": MAX_STEPS,
    })
    yield {"phase": "final", "content": final_answer, "trace_id": trace_id}


def json_loads(s: str) -> dict:
    import json
    try:
        return json.loads(s) if s else {}
    except Exception:
        return {}


def json_dumps(obj) -> str:
    import json
    try:
        return json.dumps(obj, ensure_ascii=False)
    except Exception:
        return '{"success": false, "error": "result not serializable"}'
