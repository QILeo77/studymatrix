# -*- coding: utf-8 -*-
"""工具层单元测试：get_schedule / generate_test 注册 + needs_context 派发 + 白名单 + 参数校验"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from agents.tools_registry import (
    TOOL_REGISTRY, get_tool_schemas, tool_get_schedule,
)
from agents.executor import ToolExecutor

# 1) 两个核心工具已注册 + 出现在 Function Calling schema 中
names = [t["function"]["name"] for t in get_tool_schemas()]
print("tools:", names)
assert "get_schedule" in names, "get_schedule 未注册"
assert "generate_test" in names, "generate_test 未注册"
assert "query_schedule" not in names, "旧名 query_schedule 应已重命名"
assert TOOL_REGISTRY["generate_test"].get("needs_context") is True, "generate_test 需 needs_context"

# 2) tool_get_schedule：读用户前端课表
rows = [
    {"day": "周一", "start": "08:00", "end": "09:40", "course": "高等数学"},
    {"day": "周三", "start": "14:00", "end": "15:40", "course": "数据结构"},
]
r = tool_get_schedule({}, user_schedule=rows)
print("schedule_text:", r["schedule_text"])
assert r["count"] == 2 and "高等数学" in r["schedule_text"]
assert r["source"] == "用户专属课表（前端录入）"

# 3) 未录入课表 → 兜底读本地 JSON（当前为空）→ note 提示
r2 = tool_get_schedule({}, user_schedule=[])
print("empty note:", r2["note"])
assert r2["count"] == 0 and r2["note"], "空课表应给出兜底 note"

# 4) ToolExecutor：get_schedule 派发（needs_user_schedule 注入）
ex = ToolExecutor(user_context={"user_schedule": rows, "profile": {}, "user_message": "x"})
out = ex.execute("get_schedule", {}, 1)
print("executor get_schedule count:", out.get("count"))
assert out["success"] is True and out["count"] == 2

# 5) 白名单：未注册工具被拦截
bad = ex.execute("delete_all", {}, 1)
assert bad["success"] is False and "未注册" in bad["error"]

# 6) 参数校验：generate_plan 的 weak_subject 必须是字符串
bad2 = ex.execute("generate_plan", {"weak_subject": 123}, 1)
assert bad2["success"] is False and "字符串" in bad2["error"], "参数类型校验失效"
print("bad2:", bad2["error"])

# 7) 参数校验：generate_test 的 count 超出上限被拦截
bad3 = ex.execute("generate_test", {"count": 99}, 1)
assert bad3["success"] is False and "不得大于" in bad3["error"], "count 上限校验失效"
print("bad3:", bad3["error"])

print("\n===== 工具层单元测试全部通过 =====")