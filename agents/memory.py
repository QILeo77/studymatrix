# agents/memory.py
# Memory 机制（SQLite 版）：save_memory / load_recent_memory / build_memory_prompt
# 说明：对外函数签名保持不变，内部由 data/memory.json 迁移到 data/app.db 的 memories 表。

import json

from agents.storage import execute, now_str, query_all

MAX_PER_USER = 50            # 每个用户最多保留记录数（防膨胀）
DEFAULT_CONTEXT_LIMIT = 5    # 注入 Prompt 的最近记录条数


def save_memory(user_id: str, event: dict) -> dict:
    """把一次事件写入 memories 表（提问 / 打卡情况 / AI 反馈）

    event 字段建议：
      type     : "chat"（提问+反馈） | "checkin"（打卡） | "plan" | ...
      ask      : 用户提问原文
      progress : 打卡情况/进度文本
      reply    : AI 反馈
      subjects : 涉及科目（可选）
    """
    uid = (user_id or "").strip() or "default"
    ev = dict(event or {})
    rec = {
        "time": now_str(),
        "type": (ev.get("type") or "chat"),
        "ask": (ev.get("ask") or "").strip()[:200],
        "progress": (ev.get("progress") or "").strip()[:200],
        "reply": (ev.get("reply") or "").replace("\n", " ").strip()[:220],
        "subjects": ev.get("subjects") or [],
    }
    execute(
        "INSERT INTO memories(user_id, type, ask, progress, reply, subjects, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (uid, rec["type"], rec["ask"], rec["progress"], rec["reply"],
         json.dumps(rec["subjects"], ensure_ascii=False), rec["time"]),
    )
    _trim(uid)
    return rec


def _trim(user_id: str):
    """每个用户仅保留最近 MAX_PER_USER 条"""
    try:
        execute(
            "DELETE FROM memories WHERE user_id = ? AND id NOT IN "
            "(SELECT id FROM memories WHERE user_id = ? ORDER BY id DESC LIMIT ?)",
            (user_id, user_id, MAX_PER_USER),
        )
    except Exception:
        pass


def _row_to_rec(r) -> dict:
    try:
        subjects = json.loads(r["subjects"] or "[]")
    except Exception:
        subjects = []
    return {
        "time": r["created_at"],
        "type": r["type"],
        "ask": r["ask"] or "",
        "progress": r["progress"] or "",
        "reply": r["reply"] or "",
        "subjects": subjects or [],
    }


def load_recent_memory(user_id: str, limit: int = DEFAULT_CONTEXT_LIMIT) -> list:
    """取某用户最近 limit 条记录（时间正序返回，便于阅读）"""
    uid = (user_id or "").strip() or "default"
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = DEFAULT_CONTEXT_LIMIT
    rows = query_all(
        "SELECT * FROM memories WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (uid, limit),
    )
    return [_row_to_rec(r) for r in reversed(rows)]   # 反转为时间正序


def build_memory_prompt(user_id: str, limit: int = DEFAULT_CONTEXT_LIMIT) -> str:
    """最近 N 条记忆 -> Prompt Context 块（支撑「回忆式」个性化回答）"""
    recs = load_recent_memory(user_id, limit)
    if not recs:
        return ""

    lines = [f"【长期记忆 · 最近 {len(recs)} 次互动（按时间正序）】"]
    for i, r in enumerate(recs, 1):
        if r.get("type") == "checkin":
            lines.append(f"{i}. [{r.get('time', '')}] 打卡：{r.get('progress') or '—'}")
        else:
            seg = (f"{i}. [{r.get('time', '')}] 用户说：{(r.get('ask') or '—')[:60]}"
                   f"｜我回：{(r.get('reply') or '—')[:60]}")
            if r.get("progress"):
                seg += f"｜当时进度：{r['progress']}"
            lines.append(seg)

    lines.append(
        "请在合适的场景自然引用上述记忆，给出个性化的「回忆式」回应"
        "（例如：记得你前天也卡在级数那道题上，今天再试试？）。"
        "不要生硬罗列，也不必每次都提及。"
    )
    return "\n".join(lines)