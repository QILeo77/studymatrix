# agents/planner.py
# 计划 Agent：调用课表工具 get_free_time() 获取真实空闲时间，
# 拼入系统提示词后调用 DeepSeek 生成复习计划
# 由 app.py 在 Router 识别出 "planner" 意图后调用

import os
from datetime import datetime

from dotenv import load_dotenv
from openai import OpenAI

from agents.prompts import EBBINGHAUS_PROMPT, SM2_PROMPT
from tools.schedule import get_free_time

load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com",
)

# Planner Agent 基础系统提示词（规划师人设 + 表格格式强约束）
PLANNER_PROMPT = (
    "你是一个专业的学业规划师。请用 Markdown 表格输出用户的复习计划。"
    "【表格格式 · 强制要求】表格必须且只能包含以下三列，列头固定为："
    "| 日期 | 时间段 | 任务内容 |。"
    "日期列只写日期本身（如：周一、周二、10月25日），时间段列只写时间（如：14:00-16:00），"
    "任务内容列写具体任务（如：复习高数第三章）。"
    "绝对不能把日期和时间挤在同一列里，也不能增加或减少列数。"
    "示例：\n"
    "| 日期 | 时间段 | 任务内容 |\n"
    "|---|---|---|\n"
    "| 周一 | 14:00-16:00 | 复习高数 |\n"
    "| 周一 | 19:00-20:30 | 英语听力精练 |\n"
    "| 10月25日 | 09:00-11:00 | 背单词 |"
)

# 中文星期映射（datetime.weekday(): 0=周一 ... 6=周日）
WEEKDAY_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def _get_now_str() -> str:
    """获取当前本地真实时间（精确到分钟）：'2026年10月22日 星期四 15:30'"""
    now = datetime.now()
    return (
        f"{now.year}年{now.month}月{now.day}日 "
        f"{WEEKDAY_CN[now.weekday()]} "
        f"{now.hour:02d}:{now.minute:02d}"
    )


def build_profile_prompt(profile: dict) -> str:
    """将前端提交的学习画像转成强约束提示词片段

    profile 示例: {"goal": "期末冲刺", "examDate": "2026-11-15",
                   "hours": "2-4小时", "weak": "高数"}
    """
    if not isinstance(profile, dict):
        return ""

    parts = []
    goal = (profile.get("goal") or "").strip()
    exam_date = (profile.get("examDate") or "").strip()
    hours = (profile.get("hours") or "").strip()
    weak = (profile.get("weak") or "").strip()

    if goal:
        parts.append(f"目标是{goal}")
    if exam_date:
        parts.append(f"考试日期是{exam_date}（请据此倒推剩余天数并合理安排进度）")
    if hours:
        parts.append(f"每天只有{hours}")
    if weak:
        parts.append(f"薄弱科目是{weak}")

    if not parts:
        return ""
    return (
        "【用户学习画像 · 强约束】注意：该用户"
        + "，".join(parts)
        + "。请务必据此排课与回答，不得给出超出每日可用时长的安排。"
    )


def build_system_prompt(profile: dict = None) -> str:
    """构建含真实时间 + 学习画像 + 真实空闲时间的系统提示词

    工具读取失败（返回 None）时退回基础提示词，不影响主流程。
    """
    free_time = get_free_time()  # 本地教务系统：data/schedule.json
    now_str = _get_now_str()     # 当前本地真实时间（分钟级）

    parts = [
        PLANNER_PROMPT,
        EBBINGHAUS_PROMPT,
        SM2_PROMPT,
        (
            f"【系统环境信息】：当前的真实时间是 {now_str}。"
            f"请结合这个具体时间点，合理规划用户的学习任务。"
            f"如果用户要求排今天的计划且时间已晚，请灵活调整。"
        ),
    ]
    if profile:
        profile_prompt = build_profile_prompt(profile)
        if profile_prompt:
            parts.append(profile_prompt)
    if free_time:
        parts.append(f"已知该用户的真实空闲时间为：{free_time}，请根据这些时间回答用户。")
    return "\n\n".join(parts)


def make_plan(user_message: str, profile: dict = None) -> str:
    """生成复习计划

    Args:
        user_message: 用户原始消息
        profile: 学习画像 {goal, hours, weak}，来自前端配置区

    Returns:
        Markdown 格式的复习计划文本
    """
    system_prompt = build_system_prompt(profile)

    response = _client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        stream=False,
    )
    return response.choices[0].message.content or ""
