# tools/schedule.py
# 课表工具：读取 data/schedule.json，计算每日空闲时间段
# 对外主接口：get_free_time() —— 供 /api/chat 在 planner 意图时调用

import json
from pathlib import Path

SCHEDULE_PATH = Path(__file__).resolve().parent.parent / "data" / "schedule.json"


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
    """加载课表数据（文件不存在 / JSON 非法时抛出异常）"""
    with open(SCHEDULE_PATH, encoding="utf-8") as f:
        return json.load(f)


def get_free_slots(day: str = None, min_minutes: int = 30) -> list:
    """计算空闲时间段

    Args:
        day: 星期（如 '周一'）；为 None 时返回全部 5 天
        min_minutes: 空闲段最小时长过滤（默认 30 分钟）

    Returns:
        [{"day": "周一", "start": "09:40", "end": "10:00", "duration": "0.3小时"}, ...]
    """
    data = load_schedule()
    week = data.get("week_schedule", {})
    bounds = data.get("daily_bounds", {"start": "08:00", "end": "22:00"})

    b_start = _to_minutes(bounds["start"])
    b_end = _to_minutes(bounds["end"])

    days = [day] if day else list(week.keys())
    free_slots = []

    for d in days:
        courses = week.get(d, [])
        # 按开始时间排序
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


def get_free_time():
    """读取 data/schedule.json，返回空闲时间的中文描述文本

    返回示例:
        "周一 09:40-10:00、11:40-14:00、15:40-22:00 空闲，周二 09:40-10:00、11:40-22:00 空闲，..."
        （某天无课时返回 "周X全天空闲"）

    Returns:
        成功: 空闲时间描述文本
        失败: None（同时在控制台打印错误）
    """
    try:
        data = load_schedule()
    except Exception as e:
        print(f"[Tool Error] schedule.json 读取失败: {e}")
        return None

    week = data.get("week_schedule", {})
    bounds = data.get("daily_bounds") or {"start": "08:00", "end": "22:00"}

    parts = []
    for day in week:
        slots = get_free_slots(day=day, min_minutes=30)
        # 全天空闲：唯一一段且覆盖整个每日边界
        if (
            len(slots) == 1
            and slots[0]["start"] == bounds["start"]
            and slots[0]["end"] == bounds["end"]
        ):
            parts.append(f"{day}全天空闲")
        elif slots:
            ranges = "、".join(f"{s['start']}-{s['end']}" for s in slots)
            parts.append(f"{day} {ranges} 空闲")
        else:
            parts.append(f"{day}无空闲时间")

    if not parts:
        print("[Tool Error] schedule.json 中未找到课表数据（week_schedule 为空）")
        return None

    return "，".join(parts)


if __name__ == "__main__":
    # 直接运行本文件可快速验证工具输出
    print(get_free_time())
