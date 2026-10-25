# -*- coding: utf-8 -*-
"""督学反馈机制测试（基于本周计划完成率）
  1) 单元：反馈约束生成 + 完成率解析
  2) 端到端：完成率 2/10 → 批评 + 25 分钟番茄钟；完成率 9/10 → 表扬 + 薄弱科目加量 20%
"""
import json
import sys
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

out = []

from agents.prompts import build_tutor_feedback_prompt, TUTOR_LOW_THRESHOLD, TUTOR_HIGH_THRESHOLD
import app as A

# ---------- 1) 单元 ----------
low = build_tutor_feedback_prompt(2, 10, None)
high = build_tutor_feedback_prompt(9, 10, None)
mid = build_tutor_feedback_prompt(6, 10, None)
out.append("[单元] 阈值: low<%.1f high>%.1f" % (TUTOR_LOW_THRESHOLD, TUTOR_HIGH_THRESHOLD))
out.append("[单元] 2/10 →\n" + low)
out.append("[单元] 9/10 →\n" + high)
assert "20%" in low and "25 分钟" in low and "批评" in low, "低完成率约束不完整"
assert "20%" in high and "表扬" in high and "薄弱科目" in high, "高完成率约束不完整"
assert "50%~80%" in mid and "30~45" in mid, "中区间约束缺失"
assert build_tutor_feedback_prompt(None, None, None) == "", "无数据应为空"

cases = [
    ({"completion_done": 2, "completion_total": 10}, (2, 10, 0.2)),
    ({"completion": "3/4"}, (3, 4, 0.75)),
    ({"completion_rate": 0.9}, (None, None, 0.9)),
    ({"completion_rate": "0.35", "completion": "7/20"}, (7, 20, 0.35)),
    ({}, (None, None, None)),
]
for payload, expect in cases:
    got = A.parse_completion(payload)
    out.append(f"[单元] parse_completion({payload}) -> {got}  (期望 {expect})")
    assert got == expect, (payload, got, expect)
out.append("[单元] tutor_mode_of: 0.2→%s  0.9→%s  0.6→%s  None→%r" % (
    A.tutor_mode_of(0.2), A.tutor_mode_of(0.9), A.tutor_mode_of(0.6), A.tutor_mode_of(None)))
assert A.tutor_mode_of(0.2) == "harsh" and A.tutor_mode_of(0.9) == "praise"

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
    "user_id": "u_tutortest",
    "profile": {"goal": "期末冲刺", "weak": "高数",
                "subjects": [{"subject": "高数", "examDate": ""}]},
    "schedule_rows": [{"day": "周一", "start": "08:00", "end": "09:40", "course": "高等数学"}],
}

# 2a) 完成率 2/10 → 批评 + 25 分钟番茄钟
e1, r1 = post_chat(dict(BASE_PAYLOAD, message="帮我生成新的复习计划",
                        completion="2/10", completion_done=2, completion_total=10))
router1 = [d for en, d in e1 if en == "router"]
out.append("\n[端到端·2/10] router tutor_mode: " + str([d.get("tutor_mode") for d in router1]))
out.append("[端到端·2/10] 回复片段:\n" + r1[:520])
ok1 = any(k in r1 for k in ["25 分钟", "25分钟", "番茄"]) and any(
    k in r1 for k in ["批评", "执行力", "拖延", "严厉", "别", "堆积"])
out.append("[端到端·2/10] 命中 番茄钟+批评: " + str(ok1))
assert ok1, r1[:300]

# 2b) 完成率 9/10 → 表扬 + 薄弱科目加量 20%
e2, r2 = post_chat(dict(BASE_PAYLOAD, message="帮我生成新的复习计划",
                        completion="9/10", completion_done=9, completion_total=10))
out.append("\n[端到端·9/10] router tutor_mode: " + str([d.get("tutor_mode") for en, d in e2 if en == "router"]))
out.append("[端到端·9/10] 回复片段:\n" + r2[:520])
ok2 = any(k in r2 for k in ["20%", "表扬", "状态神勇", "加量", "增加"]) and any(
    k in r2 for k in ["表扬", "神勇", "肯定", "棒", "不错"])
out.append("[端到端·9/10] 命中 表扬+加量: " + str(ok2))
assert ok2, r2[:300]

out.append("\n===== 督学反馈机制测试全部通过 =====")
Path(BASE / "_tutor_out.txt").write_text("\n".join(out), encoding="utf-8")
print("done. low=", ok1, "high=", ok2)