# agents/planner.py
# 计划 Agent：调用课表工具 get_free_time() 获取真实空闲时间，
# 拼入系统提示词后调用 DeepSeek 生成复习计划
# 由 app.py 在 Router 识别出 "planner" 意图后调用

import os

from dotenv import load_dotenv
from openai import OpenAI

from tools.schedule import get_free_time

load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com",
)

# Planner Agent 基础系统提示词（规划师人设）
PLANNER_PROMPT = (
    "你是一个专业的学业规划师。"
    "请用 Markdown 表格输出用户的复习计划，包含时间、任务、目标三列。"
)


def build_system_prompt() -> str:
    """构建含真实空闲时间的系统提示词

    工具读取失败（返回 None）时退回基础提示词，不影响主流程。
    """
    free_time = get_free_time()  # 本地教务系统：data/schedule.json
    if free_time:
        return (
            f"{PLANNER_PROMPT}\n\n"
            f"已知该用户的真实空闲时间为：{free_time}，请根据这些时间回答用户。"
        )
    return PLANNER_PROMPT


def make_plan(user_message: str) -> str:
    """生成复习计划

    Args:
        user_message: 用户原始消息

    Returns:
        Markdown 格式的复习计划文本
    """
    system_prompt = build_system_prompt()

    response = _client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        stream=False,
    )
    return response.choices[0].message.content or ""
