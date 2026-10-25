# -*- coding: utf-8 -*-
"""端到端：验证「大模型自主决定调用外部工具」（无 if 判断）
- 问课表 → 期望决策调用 get_schedule
- 要出题 → 期望决策调用 generate_test
- 要计划 → 期望决策调用 generate_plan
"""
import json
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
URL = "http://127.0.0.1:5000/api/chat"


def post_chat(payload):
    req = urllib.request.Request(
        URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    events, final = [], ""
    with urllib.request.urlopen(req, timeout=180) as resp:
        buf = ""
        for raw in resp:
            buf += raw.decode("utf-8")
            while "\n\n" in buf:
                chunk, buf = buf.split("\n\n", 1)
                ename, dstr = "", ""
                for line in chunk.split("\n"):
                    if line.startswith("event:"):
                        ename = line[6:].strip()
                    elif line.startswith("data:"):
                        dstr += line[5:].strip()
                if not ename:
                    continue
                try:
                    data = json.loads(dstr) if dstr else {}
                except Exception:
                    data = {}
                events.append((ename, data))
                if ename == "final":
                    final = data.get("reply", "")
    return events, final


def tool_decisions(events):
    seq = []
    for ename, data in events:
        if ename == "agent_step" and data.get("type") == "tool":
            seq.append((data.get("tool"), data.get("ok")))
    return seq


PAYLOAD_BASE = {
    "profile": {"goal": "期末冲刺", "weak": "高数",
                "subjects": [{"subject": "高数", "examDate": ""}]},
    "schedule_rows": [
        {"day": "周一", "start": "08:00", "end": "09:40", "course": "高等数学"},
        {"day": "周三", "start": "14:00", "end": "15:40", "course": "数据结构"},
    ],
    "progress_data": "已完成周一全部任务",
}

print(">>> 用例1：用户问课表（期望决策调用 get_schedule）")
p1 = dict(PAYLOAD_BASE)
p1["message"] = "我这周都有哪些课？周一和周三分别上什么？"
e1, f1 = post_chat(p1)
acts1 = tool_decisions(e1)
print("决策序列:", acts1)
print("回复前 120 字:", f1[:120].replace("\n", " "))
assert any(t == "get_schedule" for t, _ in acts1), f"用例1 未决策调用 get_schedule：{acts1}"

print("\n>>> 用例2：用户要出题（期望决策调用 generate_test）")
p2 = dict(PAYLOAD_BASE)
p2["message"] = "针对高数出一道题考考我"
e2, f2 = post_chat(p2)
acts2 = tool_decisions(e2)
print("决策序列:", acts2)
print("回复前 120 字:", f2[:120].replace("\n", " "))
assert any(t == "generate_test" for t, _ in acts2), f"用例2 未决策调用 generate_test：{acts2}"

print("\n>>> 用例3：用户要排计划（期望决策调用 generate_plan）")
p3 = dict(PAYLOAD_BASE)
p3["message"] = "帮我制定一份复习计划，重点补薄弱的高数。"
e3, f3 = post_chat(p3)
acts3 = tool_decisions(e3)
print("决策序列:", acts3)
print("回复前 120 字:", f3[:120].replace("\n", " "))
assert any(t == "generate_plan" for t, _ in acts3), f"用例3 未决策调用 generate_plan：{acts3}"

print("\n===== 端到端通过：大模型自主决定调用外部工具（无 if 判断）=====")