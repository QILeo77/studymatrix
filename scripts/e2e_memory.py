# -*- coding: utf-8 -*-
"""端到端：Memory 机制（save_memory + 最近5条注入 + 回忆式回答）"""
import json
import urllib.request

CHAT = "http://127.0.0.1:5000/api/chat"
CHECKIN = "http://127.0.0.1:5000/api/memory/checkin"
UID = "u_memtest"


def post(url, payload, sse=False):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    if not sse:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    final = ""
    with urllib.request.urlopen(req, timeout=180) as r:
        buf = ""
        for raw in r:
            buf += raw.decode("utf-8")
            while "\n\n" in buf:
                chunk, buf = buf.split("\n\n", 1)
                en, ds = "", ""
                for line in chunk.split("\n"):
                    if line.startswith("event:"):
                        en = line[6:].strip()
                    elif line.startswith("data:"):
                        ds += line[5:].strip()
                if en == "final" and ds:
                    final = json.loads(ds).get("reply", "")
    return final


def get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


BASE = {"user_id": UID, "profile": {"goal": "期末冲刺", "weak": "高数",
                                    "subjects": [{"subject": "高数", "examDate": ""}]}}

out = []
# 1) 第一次提问（关于「级数」，作为可回忆的记忆点）
r1 = post(CHAT, dict(BASE, message="我卡在级数这道题上了，求和函数展开老是算错，帮我看看",
                     progress_data="今日 3 项任务已完成 1 项"), sse=True)
out.append("[1] 首次提问回复: " + r1[:120].replace("\n", " "))

# 2) 打卡上报
r2 = post(CHECKIN, {"user_id": UID, "progress": "今日 3 项任务已完成 2 项", "undone": 1})
out.append("[2] 打卡上报: " + json.dumps(r2, ensure_ascii=False)[:160])

# 3) 查看最近 5 条记忆
r3 = get("http://127.0.0.1:5000/api/memory?user_id=" + UID + "&limit=5")
out.append("[3] 记忆条数: " + str(len(r3.get("records", []))))
for i, rec in enumerate(r3.get("records", []), 1):
    out.append("    {}. type={} ask={} progress={}".format(
        i, rec.get("type"), (rec.get("ask") or "")[:40], (rec.get("progress") or "")[:40]))

# 4) 追问「我最近学得怎么样」→ 期望回忆式个性化回答
r4 = post(CHAT, dict(BASE, message="我最近学得怎么样？",
                     progress_data="今日 3 项任务已完成 2 项"), sse=True)
out.append("[4] 回忆式回答: " + r4[:260].replace("\n", " "))

hit = any(k in r4 for k in ["级数", "高数", "上次", "记得", "之前", "前天", "昨天", "打卡"])
out.append("[5] 引用历史记忆: " + str(hit))

open(r"D:\编程\2026-10-20-22-57-15\_mem_out.txt", "w", encoding="utf-8").write("\n".join(out))
print("done. memory_hit=", hit)