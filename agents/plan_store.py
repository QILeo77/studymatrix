# agents/plan_store.py
# 排程 Agent 落库层（P0-2）：把 generate_plan 产出的 Markdown 计划表
# 解析为结构化 plan_item 行落库；打卡/完成率由 SQL 统计（不再靠前端拼 "2/10"）。
#
# 解析规则（与前端 renderDayPanel 同口径）：
#   表格三列 | 日期 | 时间段 | 任务内容 |
#   日期列支持：周一 / 星期一 / 10月26日 / 2026-10-26 / 今天 / 明天
#   时间段支持：14:00-16:00 / 14:00–16:00 / 19:00~20:30 / 08:00-09:40（含全角破折号）

import re
from datetime import date, datetime, timedelta

from agents.storage import execute, now_str, query_all

WEEKDAY_ALL = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
_WEEK_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}

# 科目关键词（与 agents/history.SUBJECT_KEYWORDS 对齐，截取高频）
SUBJECT_KEYWORDS = [
    "高等数学", "高数", "线性代数", "概率论", "大学英语视听说", "大学英语", "英语",
    "数据结构", "操作系统", "计算机网络", "计算机组成", "数据库", "软件工程",
    "大学物理", "物理", "Python程序设计", "C语言", "Java", "政治", "马克思",
]


def _parse_time_range(text: str):
    """时间段文本 → (start, end, duration_min) 或 (None, None, None)

    支持 14:00-16:00 / 14:00–16:00 / 14:00~16:30 / 08:00 —09:40（全角）
    """
    m = re.search(
        r"(\d{1,2}):(\d{2})\s*[-–—~～至]\s*(\d{1,2}):(\d{2})", text or "")
    if not m:
        return None, None, None
    s = int(m.group(1)) * 60 + int(m.group(2))
    e = int(m.group(3)) * 60 + int(m.group(4))
    if e <= s or e - s > 8 * 60:
        return None, None, None
    start = f"{int(m.group(1)):02d}:{m.group(2)}"
    end = f"{int(m.group(3)):02d}:{m.group(4)}"
    return start, end, e - s


def _parse_duration_text(text: str):
    """从任务文本提取时长：xx 分钟 / x.x 小时 → 分钟；无则 None"""
    mm = re.search(r"(\d{1,3})\s*分钟", text or "")
    if mm:
        return max(5, min(480, int(mm.group(1))))
    hm = re.search(r"(\d{1,2}(?:\.\d)?)\s*(?:小时|h|H)(?![a-zA-Z])", text or "")
    if hm:
        return max(5, min(480, round(float(hm.group(1)) * 60)))
    return None


def _parse_date_col(raw: str):
    """日期列 → 规范日期字符串（周一 / 2026-10-26）；无法识别返回原文 trim"""
    t = (raw or "").strip()
    if not t:
        return ""
    if re.search(r"今天|今日", t):
        return date.today().isoformat()
    if re.search(r"明天|明日", t):
        return (date.today() + timedelta(days=1)).isoformat()
    m = re.search(r"(?:周|星期)([一二三四五六日天])", t)
    if m:
        return "周" + ("日" if m.group(1) == "天" else m.group(1))
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})月(\d{1,2})日", t)
    if m:
        now = datetime.now()
        try:
            return date(now.year, int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            pass
    return t[:30]


def _guess_subject(task: str) -> str:
    """从任务文本猜科目（关键词最长命中优先）"""
    best, best_len = "", 0
    for kw in SUBJECT_KEYWORDS:
        if kw in (task or "") and len(kw) > best_len:
            best, best_len = kw, len(kw)
    return best


def _is_review_task(task: str) -> str:
    """任务类型：含复习/回顾/巩固/复盘/重刺 → review，否则 new"""
    return "review" if re.search(r"复习|回顾|巩固|复盘|重刺|巩固练习", task or "") else "new"


def parse_plan_markdown(md_text: str) -> list:
    """解析 Markdown 计划表格 → [{date, start_time, end_time, duration, subject, task, type}]

    只认三列表格（| 日期 | 时间段 | 任务内容 |）；表头行与分隔行自动跳过。
    """
    items = []
    for line in (md_text or "").splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) != 3:
            continue
        joined = "".join(cells)
        # 跳过表头行（含列名）与分隔行（--- / :---:）
        if ("日期" in joined and "时间段" in joined) or ("任务内容" in joined and "日期" in joined):
            continue
        if set(joined) <= set("-: "):
            continue
        d_raw, t_raw, task_raw = cells
        if not task_raw:
            continue
        date_col = _parse_date_col(d_raw)
        if not date_col:
            continue
        start, end, dur = _parse_time_range(t_raw or task_raw)
        if dur is None:
            dur = _parse_duration_text(task_raw)
        items.append({
            "date": date_col,
            "start_time": start or "",
            "end_time": end or "",
            "duration": dur,
            "subject": _guess_subject(task_raw) or _guess_subject(d_raw),
            "task": task_raw[:200],
            "type": _is_review_task(task_raw),
        })
    return items


def save_plan_items(user_id: str, items: list, session_id: str = "") -> int:
    """把解析后的计划条目写入 plan_item 表；返回写入行数（空计划返回 0）"""
    uid = (user_id or "").strip() or "default"
    if not items:
        return 0
    for it in items:
        execute(
            "INSERT INTO plan_item(user_id, session_id, date, start_time, end_time, "
            "duration, subject, task, type, kp_id, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,NULL,0,?)",
            (uid, (session_id or "")[:40], it.get("date", ""), it.get("start_time", ""),
             it.get("end_time", ""), it.get("duration"), it.get("subject", ""),
             it.get("task", ""), it.get("type", "new"), now_str()),
        )
    return len(items)


def store_plan(user_id: str, plan_markdown: str, session_id: str = "") -> dict:
    """组合入口：Markdown → 解析 → 落库。返回 {"count", "items"}"""
    items = parse_plan_markdown(plan_markdown)
    count = save_plan_items(user_id, items, session_id=session_id)
    return {"count": count, "items": items}


def week_completion(user_id: str) -> dict:
    """本周计划完成率（SQL 统计，督学 Agent 数据源）

    统计口径：plan_item 中 date 命中「本周一 ~ 今天」的行（周一/ISO 日期均归一化）。
    Returns: {"done", "total", "rate", "items_done", "text"}
    """
    uid = (user_id or "").strip() or "default"
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    rows = query_all(
        "SELECT date, status FROM plan_item WHERE user_id = ? ORDER BY id DESC LIMIT 500",
        (uid,),
    )
    # 本周命中的日期集合：ISO 周一~今天 + 本周中文星期名
    week_keys = set()
    d = monday
    while d <= today:
        week_keys.add(d.isoformat())
        week_keys.add("周" + "一二三四五六日"[d.weekday()])
        d += timedelta(days=1)
    picked = [r for r in rows if (r["date"] or "") in week_keys]
    total = len(picked)
    done = sum(1 for r in picked if r["status"] == 1)
    rate = (done / total) if total > 0 else None
    return {
        "done": done, "total": total, "rate": rate,
        "items_done": done,
        "text": (f"{done}/{total}" if total > 0 else ""),
    }


def mark_plan_item(plan_item_id: int, status: int, user_id: str = "") -> dict:
    """打卡：更新某条计划状态（0未开始/1完成/2跳过）"""
    try:
        status = int(status)
    except (TypeError, ValueError):
        return {"error": "status 必须是整数"}
    if status not in (0, 1, 2):
        return {"error": "status 只能是 0/1/2"}
    execute("UPDATE plan_item SET status = ? WHERE id = ?", (status, plan_item_id))
    return {"plan_item_id": plan_item_id, "status": status}


def list_recent_items(user_id: str, limit: int = 50) -> list:
    """最近计划条目（看板/调试用）"""
    uid = (user_id or "").strip() or "default"
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 50
    return [dict(r) for r in query_all(
        "SELECT id, date, start_time, end_time, duration, subject, task, type, status, "
        "created_at FROM plan_item WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (uid, limit),
    )]
