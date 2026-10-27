# agents/review.py
# 复习 Agent（出刺）：艾宾浩斯 + SM-2 动态间隔 —— 真实算法落库版（P0-1）
#   数据：SQLite knowledge_point / review_log 表（agents/storage.py 自动建表）
#   能力：
#     register_kp()       登记知识点 → 生成 5 档复习计划（第 1 天必刺）
#     daily_targets()     今日必刺清单（next_review_date <= today 且未完成）
#     submit_score()      记忆自评 1~5 → review_log 落库 + adjust_interval 动态调整
#     adjust_interval()   SM-2 简化版：≥4 ×1.5 延后；≤2 ×0.5 提前并重刺；=3 不变
#   对应参赛方案 3.2「按时出刺：艾宾浩斯复习调度引擎」

import json
from datetime import date, datetime, timedelta

from agents.storage import execute, now_str, query_all, query_one

# 初始间隔序列（SM-2 初始档位）：学习后第 1/2/4/7/15 天，共 5 档
DEFAULT_INTERVALS = [1, 2, 4, 7, 15]
# 学科特性序列（与 tools_registry.REVIEW_INTERVALS 口径一致）
SUBJECT_INTERVALS = {
    "英语": [1, 2, 4, 7, 15, 30],
    "数学": [1, 2, 3, 5],
    "高等数学": [1, 2, 3, 5],
    "高数": [1, 2, 3, 5],
}
MAX_PER_USER = 200      # 每用户知识点上限（防膨胀）


def _norm_intervals(subject: str) -> list:
    """按学科特性取初始间隔序列（未命中用默认 5 档）"""
    for key, seq in SUBJECT_INTERVALS.items():
        if key in (subject or ""):
            return list(seq)
    return list(DEFAULT_INTERVALS)


def register_kp(user_id: str, subject: str, content: str,
                first_learn_date: str = "") -> dict:
    """登记知识点 → 生成复习计划（第一刺 = first_learn_date + 第 1 档间隔）

    Args:
        subject: 科目（如 高数）
        content: 知识点内容（如 级数敛散性判别）
        first_learn_date: 首次学习日期 YYYY-MM-DD（缺省 = 今天）
    Returns:
        {"kp_id", "subject", "content", "next_review_date", "review_dates": [...], "intervals"}
    """
    uid = (user_id or "").strip() or "default"
    subject = (subject or "").strip() or "综合"
    content = (content or "").strip()[:200]
    if not content:
        return {"error": "知识点内容不能为空"}

    if first_learn_date:
        try:
            base = datetime.strptime(first_learn_date, "%Y-%m-%d").date()
        except ValueError:
            base = date.today()
    else:
        base = date.today()

    intervals = _norm_intervals(subject)
    # 5 档复习日期序列（供展示；下一步出刺日期 = 第 1 档）
    review_dates = [(base + timedelta(days=d)).isoformat() for d in intervals]

    next_due = (base + timedelta(days=intervals[0])).isoformat()
    kp_id = execute(
        "INSERT INTO knowledge_point(user_id, subject, content, first_learn_date, "
        "next_review_date, review_stage, review_intervals, done, created_at) "
        "VALUES (?,?,?,?,?,?,?,0,?)",
        (uid, subject, content, base.isoformat(), next_due,
         0, json.dumps(intervals), now_str()),
    )
    _trim(uid)
    return {
        "kp_id": kp_id, "subject": subject, "content": content,
        "first_learn_date": base.isoformat(),
        "next_review_date": next_due,
        "review_dates": review_dates,
        "intervals": intervals,
    }


def _row_to_kp(r) -> dict:
    try:
        intervals = json.loads(r["review_intervals"] or "[]")
    except Exception:
        intervals = []
    return {
        "kp_id": r["id"], "subject": r["subject"], "content": r["content"],
        "first_learn_date": r["first_learn_date"],
        "next_review_date": r["next_review_date"],
        "review_stage": r["review_stage"],
        "intervals": intervals or DEFAULT_INTERVALS,
        "done": bool(r["done"]),
    }


def daily_targets(user_id: str, days_ahead: int = 0) -> list:
    """今日必刺清单（硬约束，优先级最高）

    返回 next_review_date <= today + days_ahead 且未完成的知识点（时间正序）。
    days_ahead=0 即「截至今天」；7 = 未来一周到期。
    """
    uid = (user_id or "").strip() or "default"
    horizon = (date.today() + timedelta(days=max(0, int(days_ahead)))).isoformat()
    rows = query_all(
        "SELECT * FROM knowledge_point WHERE user_id = ? AND done = 0 "
        "AND next_review_date <= ? ORDER BY next_review_date, id",
        (uid, horizon),
    )
    return [_row_to_kp(r) for r in rows]


def get_kp(user_id: str, subject: str, content: str) -> dict:
    """定位知识点（科目+内容精确匹配；找不到返回 None）"""
    uid = (user_id or "").strip() or "default"
    row = query_one(
        "SELECT * FROM knowledge_point WHERE user_id = ? AND subject = ? "
        "AND content = ? AND done = 0 ORDER BY id DESC LIMIT 1",
        (uid, (subject or "").strip(), (content or "").strip()),
    )
    return _row_to_kp(row) if row else None


def adjust_interval(intervals: list, stage: int, memory_score: int):
    """SM-2 简化版：根据记忆自评调整「下一档」间隔

    Args:
        intervals: 当前实际间隔序列（会就地修改）
        stage: 当前已完成档位（0~4）
        memory_score: 1~5 记忆自评
    Returns:
        (new_intervals, interval_before, interval_after, restrike, back_to_stage1)
    """
    score = max(1, min(5, int(memory_score)))
    nxt = stage + 1
    if nxt >= len(intervals):
        return list(intervals), None, None, False, False
    before = intervals[nxt]
    factor = 1.5 if score >= 4 else (0.5 if score <= 2 else 1.0)
    after = max(1, round(before * factor))
    intervals[nxt] = after
    restrike = score <= 2
    back_to_stage1 = score == 1
    if back_to_stage1:
        # 1 分：整条知识点回炉到第 1 天档重走完整序列
        intervals = list(DEFAULT_INTERVALS)
    return intervals, before, after, restrike, back_to_stage1


def submit_score(user_id: str, subject: str, content: str,
                 memory_score: int) -> dict:
    """提交一次记忆自评：review_log 落库 + 推进档位 + SM-2 动态调整下次出刺日期

    Returns:
        {"kp_id", "stage_before", "stage_after", "interval_before",
         "interval_after", "next_review_date", "restrike", "back_to_stage1",
         "completed"} 或 {"error": ...}
    """
    uid = (user_id or "").strip() or "default"
    kp = get_kp(uid, subject, content)
    if not kp:
        return {"error": f"未找到知识点「{subject}/{content}」，请先登记"}

    try:
        score = int(memory_score)
    except (TypeError, ValueError):
        return {"error": "memory_score 必须是 1~5 整数"}
    if not (1 <= score <= 5):
        return {"error": "memory_score 必须是 1~5 整数"}

    stage_before = kp["review_stage"]
    intervals = list(kp["intervals"])
    today = date.today()

    # 未提供自评字段兜底：默认走满 5 档
    new_intervals, iv_before, iv_after, restrike, back1 = adjust_interval(
        intervals, stage_before, score)

    if back1:
        # 回炉：档位清零，下次 = 今天 + 第 1 档（1 天）
        stage_after = 0
        next_due = (today + timedelta(days=new_intervals[0])).isoformat()
    else:
        stage_after = stage_before + 1
        if stage_after >= len(new_intervals):
            # 完成 5 档：知识点出刺完毕
            execute(
                "UPDATE knowledge_point SET review_stage = ?, review_intervals = ?, "
                "done = 1, next_review_date = '' WHERE id = ?",
                (stage_after, json.dumps(new_intervals), kp["kp_id"]),
            )
            execute(
                "INSERT INTO review_log(kp_id, user_id, review_date, memory_score, "
                "interval_before, interval_after, created_at) VALUES (?,?,?,?,?,?,?)",
                (kp["kp_id"], uid, today.isoformat(), score, iv_before, iv_after, now_str()),
            )
            return {
                "kp_id": kp["kp_id"], "subject": subject, "content": content,
                "stage_before": stage_before, "stage_after": stage_after,
                "interval_before": iv_before, "interval_after": iv_after,
                "next_review_date": "", "restrike": restrike,
                "back_to_stage1": False, "completed": True,
                "message": "🎉 该知识点已完成全部 5 档艾宾浩斯复习，刺杀成功！",
            }
        next_due = (today + timedelta(days=new_intervals[stage_after])).isoformat()

    execute(
        "UPDATE knowledge_point SET review_stage = ?, review_intervals = ?, "
        "next_review_date = ? WHERE id = ?",
        (stage_after, json.dumps(new_intervals), next_due, kp["kp_id"]),
    )
    execute(
        "INSERT INTO review_log(kp_id, user_id, review_date, memory_score, "
        "interval_before, interval_after, created_at) VALUES (?,?,?,?,?,?,?)",
        (kp["kp_id"], uid, today.isoformat(), score, iv_before, iv_after, now_str()),
    )
    return {
        "kp_id": kp["kp_id"], "subject": subject, "content": content,
        "stage_before": stage_before, "stage_after": stage_after,
        "interval_before": iv_before, "interval_after": iv_after,
        "next_review_date": next_due, "restrike": restrike,
        "back_to_stage1": back1, "completed": False,
        "message": (
            f"已按 SM-2 调整：第 {stage_before + 1} 刺完成，"
            f"自评 {score} 分 → 下次间隔 {iv_before} → {iv_after} 天，"
            f"下次出刺日 {next_due}"
            + ("（重刺：该知识点需要重新强化）" if restrike else "")
        ),
    }


def list_kps(user_id: str, limit: int = 50) -> list:
    """列出某用户全部知识点（看板/调试用，时间倒序）"""
    uid = (user_id or "").strip() or "default"
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 50
    rows = query_all(
        "SELECT * FROM knowledge_point WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (uid, limit),
    )
    return [_row_to_kp(r) for r in rows]


def _trim(user_id: str):
    """每用户仅保留最近 MAX_PER_USER 条知识点"""
    try:
        execute(
            "DELETE FROM knowledge_point WHERE user_id = ? AND id NOT IN "
            "(SELECT id FROM knowledge_point WHERE user_id = ? ORDER BY id DESC LIMIT ?)",
            (user_id, user_id, MAX_PER_USER),
        )
    except Exception:
        pass
