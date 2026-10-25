# -*- coding: utf-8 -*-
"""历史记忆模块单元测试（SQLite 版）"""
import sys
import uuid
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import py_compile
py_compile.compile(str(BASE / "app.py"), doraise=True)
print("[OK] app.py 语法校验通过")

from agents import history as H

UID = "u_test_hist_" + uuid.uuid4().hex[:8]

# 1) 新用户历史为空 → 无注入
assert H.load_history(UID) == [], "新用户历史应为空"
assert H.build_history_prompt(UID) == "", "空历史注入应为空"

# 2) 提取科目
reply = "周一 复习「高数 · 第二章 导数应用」，周二 数据结构·二叉树遍历，英语 Unit5"
subs = H.extract_subjects(reply, profile={"subjects": [{"subject": "线性代数"}]},
                          schedule_rows=[{"course": "操作系统"}])
print("提取科目:", subs)
assert "高数" in subs and "数据结构" in subs and "英语" in subs
assert "线性代数" in subs and "操作系统" in subs

# 3) 追加记录（SQLite）
H.append_record("planner", subs, "已完成周一全部任务、周二 2/3 任务", reply,
                weak="高数", user_id=UID)
recs = H.load_history(UID)
assert len(recs) == 1, f"应有 1 条记录，实际 {len(recs)}"
assert recs[0]["subjects"] == subs
assert "已完成周一" in recs[0]["progress"]
print("[OK] 追加记录:", recs[0]["intent"], recs[0]["subjects"])

# 4) 注入提示词（连贯性）
prompt = H.build_history_prompt(UID)
print("历史注入提示词:\n" + prompt)
assert "历史复习记忆" in prompt and "上次复习科目" in prompt
assert "上次打卡进度" in prompt and "连贯" in prompt
assert "第 1 次生成计划" in prompt

# 5) 上限截断（每用户 20 条）
for i in range(25):
    H.append_record("planner", ["高数"], f"进度{i}", "x", user_id=UID)
assert len(H.load_history(UID, limit=100)) == H.MAX_RECORDS, \
    f"应截断到 {H.MAX_RECORDS}，实际 {len(H.load_history(UID, limit=100))}"
print("[OK] 每用户截断上限:", H.MAX_RECORDS)

print("\n===== 历史记忆单元测试全部通过（SQLite）=====")