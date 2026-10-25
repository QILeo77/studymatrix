# agents/history.py
# 用户历史记忆（连贯性）—— SQLite 版
#   1) 计划生成后写入 user_history 表（科目 + 打卡进度）
#   2) 下次排计划时读取历史，摘要式注入 Prompt
# 对外签名保持兼容：build_history_prompt() 可无参调用；新增可选 user_id 实现按用户隔离。

import json

from agents.storage import execute, now_str, query_all

MAX_RECORDS = 20      # 每个用户只保留最近 N 条，防膨胀

# 常见科目词表（从计划文本中提取科目用）
SUBJECT_KEYWORDS = [
    "高等数学", "高数", "线性代数", "概率论", "大学英语视听说", "大学英语", "英语",
    "数据结构", "操作系统", "计算机网络", "计算机组成", "数据库", "软件工程",
    "大学物理", "物理", "Python程序设计", "C语言", "Java", "政治", "马克思",
    "体育", "思修",
]


def _row_to_rec(r) -> dict:
    try:
        subjects = json.loads(r["subjects"] or "[]")
    except Exception:
        subjects = []
    return {
        "time": r["created_at"],
        "intent": r["intent"] or "",
        "subjects": subjects or [],
        "progress": r["progress"] or "",
        "weak": r["weak"] or "",
        "plan_excerpt": r["plan_excerpt"] or "",
    }


def load_history(user_id: str = None, limit: int = MAX_RECORDS) -> list:
    """读取历史记录（时间正序）

    user_id 为空时返回全局最近 limit 条（兼容旧调用）。
    """
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = MAX_RECORDS
    if user_id:
        rows = query_all(
            "SELECT * FROM user_history WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        )
    else:
        rows = query_all(
            "SELECT * FROM user_history ORDER BY id DESC LIMIT ?", (limit,)
        )
    return [_row_to_rec(r) for r in reversed(rows)]


def save_history(records: list):
    """整表重建（仅用于迁移/测试；生产请用 append_record）"""
    try:
        execute("DELETE FROM user_history")
        for rec in (records or []):
            execute(
                "INSERT INTO user_history(user_id, intent, subjects, progress, weak, "
                "plan_excerpt, created_at) VALUES (?,?,?,?,?,?,?)",
                (rec.get("user_id") or "default", rec.get("intent", ""),
                 json.dumps(rec.get("subjects") or [], ensure_ascii=False),
                 rec.get("progress", ""), rec.get("weak", ""),
                 rec.get("plan_excerpt", ""), rec.get("time") or now_str()),
            )
    except Exception as e:
        print(f"[History] 保存失败: {e}")


def extract_subjects(reply: str, profile: dict = None, schedule_rows: list = None) -> list:
    """提取本次计划涉及的科目（去重）

    来源优先级：计划文本常见科目词 > 学习画像科目 > 课表课程名
    """
    subs = []
    text = reply or ""

    for kw in SUBJECT_KEYWORDS:
        if kw in text and kw not in subs:
            subs.append(kw)
    for s in (profile or {}).get("subjects", []) or []:
        name = (s.get("subject") or "").strip()
        if name and name not in subs:
            subs.append(name)
    for r in schedule_rows or []:
        name = (r.get("course") or "").strip()
        if name and name not in subs:
            subs.append(name)

    return subs[:8]


def append_record(intent: str, subjects: list, progress_data: str, reply: str,
                  weak: str = "", user_id: str = None) -> dict:
    """追加一条历史记录（计划生成后调用）"""
    uid = (user_id or "").strip() or "default"
    rec = {
        "time": now_str(),
        "intent": intent,
        "subjects": subjects or [],
        "progress": (progress_data or "").strip(),
        "weak": (weak or "").strip(),
        "plan_excerpt": (reply or "").replace("\n", " ")[:160],
    }
    try:
        execute(
            "INSERT INTO user_history(user_id, intent, subjects, progress, weak, "
            "plan_excerpt, created_at) VALUES (?,?,?,?,?,?,?)",
            (uid, rec["intent"], json.dumps(rec["subjects"], ensure_ascii=False),
             rec["progress"], rec["weak"], rec["plan_excerpt"], rec["time"]),
        )
        _trim(uid)
    except Exception as e:
        print(f"[History] 记录失败: {e}")
    return rec


def _trim(user_id: str):
    try:
        execute(
            "DELETE FROM user_history WHERE user_id = ? AND id NOT IN "
            "(SELECT id FROM user_history WHERE user_id = ? ORDER BY id DESC LIMIT ?)",
            (user_id, user_id, MAX_RECORDS),
        )
    except Exception:
        pass


def build_history_prompt(user_id: str = None) -> str:
    """历史记忆 → 提示词块（摘要式注入，实现「知道用户之前复习到哪了」）"""
    records = load_history(user_id)
    if not records:
        return ""

    last = records[-1]
    parts = [
        "【历史复习记忆 · 连贯性】本次为该用户第 " + str(len(records)) + " 次生成计划。",
    ]
    if last.get("time"):
        parts.append("- 上次生成时间：" + str(last["time"]))
    if last.get("subjects"):
        parts.append("- 上次复习科目：" + "、".join(last["subjects"]))
    if last.get("progress"):
        parts.append("- 上次打卡进度：" + str(last["progress"]))
    if last.get("weak"):
        parts.append("- 用户薄弱科目：" + str(last["weak"]))
    if last.get("plan_excerpt"):
        parts.append("- 上次计划要点：" + str(last["plan_excerpt"]))

    recent_subs = []
    for r in records[-3:]:
        for s in (r.get("subjects") or []):
            if s not in recent_subs:
                recent_subs.append(s)
    if recent_subs:
        parts.append("- 近期涉及科目：" + "、".join(recent_subs[:8]))

    parts.append(
        "请基于以上历史状态继续推进：已完成的知识点不要重复安排，"
        "接着上次的进度往后排，保持复习连贯性。"
    )
    return "\n".join(parts)