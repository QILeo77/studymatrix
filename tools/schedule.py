# tools/schedule.py
# 课表工具：计算空闲时间段
# 数据源优先级：请求上下文传入的 user_schedule（前端动态列表）
#           > data/schedule.json（演示种子，仅当上下文无数据时兜底）
# 对外主接口：
#   get_free_slots(day, min_minutes, user_schedule=None)
#   get_free_time(user_schedule=None)

import json
from pathlib import Path

SCHEDULE_PATH = Path(__file__).resolve().parent.parent / "data" / "schedule.json"

# 每日计算边界（无课日视为 08:00-22:00 全天空闲）
DAY_BOUNDS = {"start": "08:00", "end": "22:00"}

WEEKDAY_ALL = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _to_minutes(t: str) -> int:
    """'09:40' -> 580"""
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def _to_hhmm(minutes: int) -> str:
    """580 -> '09:40'"""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _format_duration(minutes: int) -> str:
    """90 -> '1.5小时'"""
    hours = minutes / 60
    return f"{hours:g}小时"


def load_schedule() -> dict:
    """加载演示课表数据（文件不存在 / JSON 非法时抛出异常）"""
    with open(SCHEDULE_PATH, encoding="utf-8") as f:
        return json.load(f)


def _courses_from_user_schedule(user_schedule) -> dict:
    """把前端 user_schedule 行列表转成 {day: [{start,end,course}]} 结构"""
    week = {}
    if not user_schedule:
        return week
    for it in user_schedule:
        day = (it.get("day") or "").strip()
        start = (it.get("start") or "").strip()
        end = (it.get("end") or "").strip()
        course = (it.get("course") or "").strip()
        if not (day and start and end):
            continue
        week.setdefault(day, []).append({
            "start": start, "end": end, "course": course or "课程",
        })
    return week


def get_free_slots(day: str = None, min_minutes: int = 30, user_schedule=None) -> list:
    """计算空闲时间段

    Args:
        day: 星期（如 '周一'）；为 None 时返回全部 7 天
        min_minutes: 空闲段最小时长过滤（默认 30 分钟）
        user_schedule: 前端录入的课程行 [{day,start,end,course}]；
                       传 None 时回退读取 data/schedule.json（演示数据）

    Returns:
        [{"day": "周一", "start": "09:40", "end": "10:00", "duration": "0.3小时"}, ...]
        user_schedule 为空列表 [] 时返回 []（用户未配置课表，无数据可算）
    """
    # 数据源：优先用户动态课表；None（未传）才读演示 JSON
    if user_schedule is not None:
        week = _courses_from_user_schedule(user_schedule)
        if not week:
            return []      # 显式空课表：无空闲数据（前端应走兜底策略）
        days_covered = WEEKDAY_ALL
    else:
        data = load_schedule()
        week = data.get("week_schedule", {})
        days_covered = list(week.keys())

    b_start = _to_minutes(DAY_BOUNDS["start"])
    b_end = _to_minutes(DAY_BOUNDS["end"])

    days = [day] if day else days_covered
    free_slots = []

    for d in days:
        courses = week.get(d, [])
        cs = sorted(courses, key=lambda c: _to_minutes(c["start"]))
        prev = b_start
        for c in cs:
            gap = _to_minutes(c["start"]) - prev
            if gap >= min_minutes:
                free_slots.append({
                    "day": d,
                    "start": _to_hhmm(prev),
                    "end": _to_hhmm(_to_minutes(c["start"])),
                    "duration": _format_duration(gap),
                })
            prev = max(prev, _to_minutes(c["end"]))
        # 收尾：最后一节课到每日边界
        gap = b_end - prev
        if gap >= min_minutes:
            free_slots.append({
                "day": d,
                "start": _to_hhmm(prev),
                "end": _to_hhmm(b_end),
                "duration": _format_duration(gap),
            })

    return free_slots


def get_free_time(user_schedule=None):
    """返回空闲时间的中文描述文本

    Args:
        user_schedule: 前端课程行；None 时读演示 JSON

    Returns:
        成功: 空闲时间描述文本
        无数据（显式空课表）: None（前端走兜底，不发课表文本）
    """
    try:
        if user_schedule is not None:
            week = _courses_from_user_schedule(user_schedule)
            if not week:
                return None      # 用户未配置课表：明确无数据
        else:
            data = load_schedule()
            week = data.get("week_schedule", {})
    except Exception as e:
        print(f"[Tool Error] 课表读取失败: {e}")
        return None

    parts = []
    for day in week:
        slots = get_free_slots(day=day, min_minutes=30, user_schedule=user_schedule)
        if (
            len(slots) == 1
            and slots[0]["start"] == DAY_BOUNDS["start"]
            and slots[0]["end"] == DAY_BOUNDS["end"]
        ):
            parts.append(f"{day}全天空闲")
        elif slots:
            ranges = "、".join(f"{s['start']}-{s['end']}" for s in slots)
            parts.append(f"{day} {ranges} 空闲")
        else:
            parts.append(f"{day}无空闲时间")

    if not parts:
        return None

    return "，".join(parts)


if __name__ == "__main__":
    # 直接运行：演示 JSON 模式
    print(get_free_time())
    # 模拟前端动态课表
    demo = [
        {"day": "周一", "start": "08:00", "end": "09:40", "course": "高等数学"},
        {"day": "周一", "start": "14:00", "end": "15:40", "course": "大学物理"},
    ]
    print(get_free_time(user_schedule=demo))
    print(get_free_time(user_schedule=[]))   # 空课表 -> None
