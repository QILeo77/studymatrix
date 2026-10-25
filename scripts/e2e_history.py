# -*- coding: utf-8 -*-
"""端到端：连续两次生成计划，验证 user_history.json 自动落盘 + 累积"""
import json
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
HIST = BASE / "data" / "user_history.json"
URL = "http://127.0.0.1:5000/api/chat"


def post_chat(payload):
    req = urllib.request.Request(
        URL, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    final = None
    with urllib.request.urlopen(req, timeout=120) as resp:
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if line.startswith("data:"):
                try:
                    ev = json.loads(line[5:].strip())
                except Exception:
                    continue
                if ev.get("type") == "final":
                    final = ev.get("reply", "")
    return final or ""


payload1 = {
    "message": "帮我制定复习计划，想在高数第三章之后继续复习，薄弱科目：高数。",
    "profile": {"goal": "期末冲刺",
                "subjects": [{"subject": "高数", "examDate": ""},
                             {"subject": "英语", "examDate": ""}],
                "weak": "高数"},
    "progress_data": "已完成周一全部任务，高数复习到第三章",
    "schedule_rows": [{"day": "周一", "start": "08:00", "end": "09:40", "course": "数据结构"}],
    "plan_slots": ["10月24日 下午"],
}

print(">>> 第 1 次生成计划 ...")
r1 = post_chat(payload1)
print("回复前 200 字:", r1[:200].replace("\n", " "))
assert HIST.exists(), "第 1 次后 user_history.json 未生成"
recs = json.loads(HIST.read_text(encoding="utf-8"))["records"]
print("记录数:", len(recs))
assert len(recs) == 1, f"应有 1 条记录，实际 {len(recs)}"
print("记录1:", json.dumps(recs[0], ensure_ascii=False))
assert recs[0]["subjects"], "科目未提取"
assert recs[0]["progress"], "进度未提取"

print("\n>>> 第 2 次生成计划（应读取历史，连贯推进）...")
payload2 = dict(payload1)
payload2["message"] = "再帮我排一次计划，接着上次的进度往后推进。"
r2 = post_chat(payload2)
print("回复前 200 字:", r2[:200].replace("\n", " "))
recs = json.loads(HIST.read_text(encoding="utf-8"))["records"]
print("记录数:", len(recs))
assert len(recs) == 2, f"应有 2 条记录，实际 {len(recs)}"

print("\n===== 端到端通过：计划生成后自动落盘 + 累积 =====")
print("文件路径:", HIST)