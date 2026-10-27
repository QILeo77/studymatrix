# scripts/test_plan_store.py
# 排程 Agent 落库层单元测试：
#   1) parse_plan_markdown：三列表格 → 结构化条目（日期/时间段/时长/科目/类型）
#   2) save_plan_items：落库 plan_item 表
#   3) week_completion：SQL 统计本周完成率（周一/ISO 日期归一化）
#   4) mark_plan_item：打卡状态更新
# 运行：python scripts/test_plan_store.py （不依赖大模型 API Key）

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents import plan_store as PS

UID = "test_plan_user"

DEMO_PLAN = """好的，这是你的复习计划：

| 日期 | 时间段 | 任务内容 |
|---|---|---|
| 周一 | 14:00-16:00 | 复习高数 第三章 导数应用 |
| 周二 | 19:00~20:30 | 英语 Unit5 听力精练 45分钟 |
| 10月26日 | 09:00-11:00 | 数据结构·二叉树遍历 |
| 今天 | 20:00-21:30 | 复习线性代数：矩阵秩 |
| 明天 | 08:00-09:40 | 背单词 |

结尾说明：按艾宾浩斯间隔排布。
"""


def t1_parse():
    items = PS.parse_plan_markdown(DEMO_PLAN)
    assert len(items) == 5, f"应解析 5 条，实际 {len(items)}"

    r1 = items[0]
    assert r1["date"] == "周一", r1
    assert r1["start_time"] == "14:00" and r1["end_time"] == "16:00"
    assert r1["duration"] == 120
    assert r1["subject"] == "高数"
    assert r1["type"] == "review", "含「复习」应判为 review"

    r2 = items[1]
    assert r2["start_time"] == "19:00" and r2["end_time"] == "20:30", "全角分隔符也应识别"
    assert r2["duration"] == 90
    assert r2["subject"] == "英语"
    assert r2["type"] == "new", "「听力精练」不含复习关键词应判 new"

    r3 = items[2]
    today = date.today()
    # 10月26日按今年解析
    assert r3["date"] == date(today.year, 10, 26).isoformat(), r3["date"]

    r4 = items[3]
    assert r4["date"] == today.isoformat(), "「今天」应归一化为今日 ISO"

    r5 = items[4]
    assert r5["date"] == (today + timedelta(days=1)).isoformat(), "「明天」应归一化为明日 ISO"
    print("[OK] Markdown 解析：5 条全部正确（日期/时间/时长/科目/类型）")


def t2_store_and_completion():
    stored = PS.store_plan(UID, DEMO_PLAN)
    n = stored.get("count", 0)
    assert n == 5, f"应落库 5 条，实际 {n}"
    items = PS.list_recent_items(UID, limit=10)
    assert len(items) >= 5

    # 打卡：把今天的任务标记完成
    today_iso = date.today().isoformat()
    today_rows = [it for it in items if it["date"] == today_iso]
    assert today_rows, "「今天」行应已归一化为 ISO 并可查"
    for r in today_rows:
        res = PS.mark_plan_item(r["id"], 1)
        assert "error" not in res, res

    wc = PS.week_completion(UID)
    assert wc["total"] >= 1, "本周（含今天）至少统计到 1 条"
    assert wc["done"] >= 1
    assert wc["rate"] is not None and 0 < wc["rate"] <= 1
    print(f"[OK] 落库 + SQL 完成率：{wc['done']}/{wc['total']} = {wc['rate']:.0%}")


def t3_mark_invalid():
    bad = PS.mark_plan_item(-1, 5)
    assert "error" in bad, "非法 status 应报错"
    print("[OK] 非法打卡参数拦截")


def t4_parse_empty():
    assert PS.parse_plan_markdown("没有表格的普通回复") == []
    assert PS.parse_plan_markdown("") == []
    assert PS.store_plan(UID, "")["count"] == 0
    print("[OK] 非计划文本不落库")


if __name__ == "__main__":
    t1_parse()
    t2_store_and_completion()
    t3_mark_invalid()
    t4_parse_empty()
    print("\n===== 计划落库层单元测试全部通过 =====")
