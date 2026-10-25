# -*- coding: utf-8 -*-
"""SM-2 动态间隔算法测试：
  1) 单元：SM2_PROMPT 注入三处规划提示词 + 记忆自评解析
  2) 端到端：自评 ≥4 → 间隔×1.5（延后）；自评 ≤2 → ×0.5（提前+重刺）；备注说明
"""
import json
import sys
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

out = []

# ---------- 1) 单元 ----------
import app as A
from agents.prompts import SM2_PROMPT, build_agent_system_prompt
from agents.tools_registry import PLAN_TOOL_SYSTEM_PROMPT
import agents.planner as P

KEY = "SM-2 动态间隔算法"
out.append("[单元] AGENT_SYSTEM_PROMPT 注入 SM-2: " + str(KEY in build_agent_system_prompt({}, "周一 08:00-09:40 高数")))
out.append("[单元] PLAN_TOOL_SYSTEM_PROMPT 注入 SM-2: " + str(KEY in PLAN_TOOL_SYSTEM_PROMPT))
out.append("[单元] planner.build_system_prompt 注入 SM-2: " + str(KEY in P.build_system_prompt({})))
assert KEY in build_agent_system_prompt({}, "x") and KEY in PLAN_TOOL_SYSTEM_PROMPT
assert KEY in P.build_system_prompt({})
assert "第 1 天、第 2 天、第 4 天、第 7 天、第 15 天" in SM2_PROMPT
assert "×1.5" in SM2_PROMPT and "×0.5" in SM2_PROMPT and "重刺" in SM2_PROMPT
assert "基于您的记忆自评，为您动态调整了复习间隔" in SM2_PROMPT

cases = {"我自评 4 分": 4, "记忆自评：2/5": 2, "评分 3": 3, "这次复习自评5分": 5,
         "满分5分": None, "帮我制定复习计划": None, "第三章节还没学": None}
for text, expect in cases.items():
    got = A.detect_recall_score(text)
    out.append(f"[单元] detect_recall_score('{text}') -> {got}  (期望 {expect})")
    assert got == expect, (text, got, expect)

# ---------- 2) 端到端 ----------
URL = "http://127.0.0.1:5000/api/chat"


def post_chat(payload):
    req = urllib.request.Request(URL, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    events, final = [], ""
    with urllib.request.urlopen(req, timeout=180) as resp:
        buf = ""
        for raw in resp:
            buf += raw.decode("utf-8")
            while "\n\n" in buf:
                chunk, buf = buf.split("\n\n", 1)
                en, ds = "", ""
                for line in chunk.split("\n"):
                    if line.startswith("event:"):
                        en = line[6:].strip()
                    elif line.startswith("data:"):
                        ds += line[5:].strip()
                if not en:
                    continue
                data = json.loads(ds) if ds else {}
                events.append((en, data))
                if en == "final":
                    final = data.get("reply", "")
    return events, final


BASE_PAYLOAD = {
    "user_id": "u_sm2test",
    "profile": {"goal": "期末冲刺", "weak": "高数",
                "subjects": [{"subject": "高数", "examDate": ""}]},
    "schedule_rows": [{"day": "周一", "start": "08:00", "end": "09:40", "course": "高等数学"}],
}

# 2a) 自评 4 分 → ×1.5 延后
e1, r1 = post_chat(dict(BASE_PAYLOAD, message="帮我制定复习计划，上次复习高数我自评 4 分"))
t1 = [d.get("tool") for en, d in e1 if en == "agent_step" and d.get("type") == "tool"]
out.append("\n[端到端·自评4分] 工具决策: " + str(t1))
out.append("[端到端·自评4分] 回复片段:\n" + r1[:500])
ok1 = ("记忆自评" in r1 or "备注" in r1) and any(k in r1 for k in ["1.5", "延后", "第 10 天", "10 天"])
out.append("[端到端·自评4分] 命中 ×1.5/延后+备注: " + str(ok1))
assert ok1, r1[:300]

# 2b) 自评 2 分 → ×0.5 提前 + 重刺
e2, r2 = post_chat(dict(BASE_PAYLOAD, message="帮我排计划，上次复习自评 2 分"))
t2 = [d.get("tool") for en, d in e2 if en == "agent_step" and d.get("type") == "tool"]
out.append("\n[端到端·自评2分] 工具决策: " + str(t2))
out.append("[端到端·自评2分] 回复片段:\n" + r2[:500])
ok2 = any(k in r2 for k in ["重刺", "0.5", "提前", "第 2 天"])
out.append("[端到端·自评2分] 命中 ×0.5/重刺: " + str(ok2))
assert ok2, r2[:300]

out.append("\n===== SM-2 动态间隔算法测试全部通过 =====")
Path(BASE / "_sm2_out.txt").write_text("\n".join(out), encoding="utf-8")
print("done. score4=", ok1, "score2=", ok2)