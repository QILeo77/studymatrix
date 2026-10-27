# scripts/test_review.py
# 复习 Agent（SM-2 落库版）单元测试：
#   1) register_kp 登记知识点 → 5 档复习日期序列正确
#   2) daily_targets 今日必刺清单（含跨日到期）
#   3) submit_score 自评 4 分 → 下档间隔 ×1.5 延后
#   4) submit_score 自评 2 分 → 下档间隔 ×0.5 提前 + 重刺
#   5) submit_score 自评 1 分 → 回炉第 1 天档
#   6) 满 5 档 → done=1（刺杀成功）
# 运行：python scripts/test_review.py （不依赖大模型 API Key）

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents import review as R

UID = "test_review_user"


def t1_register():
    rec = R.register_kp(UID, "高数", "级数敛散性判别")
    assert rec["kp_id"], "kp_id 应非空"
    assert rec["intervals"] == [1, 2, 3, 5], f"数学应走加密序列，实际 {rec['intervals']}"
    assert rec["next_review_date"] == (date.today() + timedelta(days=1)).isoformat()
    assert len(rec["review_dates"]) == 4  # 数学 4 档
    print("[OK] 登记知识点：", rec["intervals"], "下次出刺", rec["next_review_date"])


def t2_daily_targets():
    # 刚登记的知识点 next_review_date = 明天 → days_ahead=1 应命中，=0 不命中
    t0 = R.daily_targets(UID, days_ahead=0)
    t1 = R.daily_targets(UID, days_ahead=1)
    assert not any(x["content"] == "级数敛散性判别" for x in t0), "今天不应到期"
    assert any(x["content"] == "级数敛散性判别" for x in t1), "明天应到期"
    print(f"[OK] 今日必刺：今日 {len(t0)} 条 / 含明天 {len(t1)} 条")


def t3_score4_extend():
    # 自评 4 → 第 2 档间隔 2 → 3（×1.5），无需重刺
    r = R.submit_score(UID, "高数", "级数敛散性判别", 4)
    assert "error" not in r, r.get("error")
    assert r["stage_after"] == 1, "应推进到第 1 档完成"
    assert r["interval_after"] == 3, f"2×1.5 应为 3，实际 {r['interval_after']}"
    assert not r["restrike"]
    assert r["next_review_date"] == (date.today() + timedelta(days=3)).isoformat()
    print("[OK] 自评4分：间隔 2→3 延后，无重刺，下次", r["next_review_date"])


def t4_score2_shrink():
    R.register_kp(UID, "英语", "虚拟语气")
    # 自评 2 → 第 2 档间隔 2 → 1（×0.5），强制重刺
    r = R.submit_score(UID, "英语", "虚拟语气", 2)
    assert "error" not in r, r.get("error")
    assert r["interval_after"] == 1, f"2×0.5 应为 1，实际 {r['interval_after']}"
    assert r["restrike"], "自评 ≤2 必须重刺"
    print("[OK] 自评2分：间隔 2→1 提前 + 重刺")


def t5_score1_back_to_stage1():
    R.register_kp(UID, "高数", "泰勒展开")
    # 连打两次推进到第 2 档，再自评 1 分 → 回炉
    R.submit_score(UID, "高数", "泰勒展开", 3)
    r = R.submit_score(UID, "高数", "泰勒展开", 1)
    assert "error" not in r, r.get("error")
    assert r["back_to_stage1"], "自评 1 分必须回炉到第 1 天档"
    assert r["stage_after"] == 0
    kp = R.get_kp(UID, "高数", "泰勒展开")
    assert kp["intervals"] == R.DEFAULT_INTERVALS, "回炉后应重置为默认 5 档"
    print("[OK] 自评1分：回炉第 1 天档，间隔序列重置")


def t6_complete():
    R.register_kp(UID, "综合", "完成测试点")
    for score in (3, 3, 3, 3, 3):   # 连打 5 次走满 5 档
        r = R.submit_score(UID, "综合", "完成测试点", score)
        assert "error" not in r, r.get("error")
    assert r.get("completed"), "5 档后应 completed=True"
    kp = R.get_kp(UID, "综合", "完成测试点")
    assert kp is None or kp["done"], "完成后 get_kp 不再返回未完成项"
    print("[OK] 满 5 档：刺杀成功（done=1）")


def t7_score3_keep():
    R.register_kp(UID, "高数", "积分中值定理")
    r = R.submit_score(UID, "高数", "积分中值定理", 3)
    assert "error" not in r, r.get("error")
    assert r["interval_after"] == r["interval_before"], "3 分间隔不变"
    print("[OK] 自评3分：间隔不变")


if __name__ == "__main__":
    t1_register()
    t2_daily_targets()
    t3_score4_extend()
    t4_score2_shrink()
    t5_score1_back_to_stage1()
    t6_complete()
    t7_score3_keep()
    print("\n===== SM-2 落库算法单元测试全部通过 =====")
