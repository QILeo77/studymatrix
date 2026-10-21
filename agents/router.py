# agents/router.py
# 路由 Agent：调用 DeepSeek 识别用户意图，将请求分发给对应的处理模块
#
#   planner -> 复习/排计划类请求，交给 agents/planner.py
#   chat    -> 普通闲聊，直接由大模型回复

import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

_client = OpenAI(
    api_key=DEEPSEEK_API_KEY,
    base_url="https://api.deepseek.com",
)

# 意图识别提示词：要求模型只输出一个单词，便于解析
ROUTER_PROMPT = (
    "你是一个意图分类器。请判断用户消息属于以下哪类意图：\n"
    "- 如果用户要求制定复习计划、安排学习时间、排课表、做学习规划，"
    "或者询问空闲时间/课表/什么时间有空，输出单词 planner\n"
    "- 其他情况（闲聊、提问、闲聊式咨询等），输出单词 chat\n"
    "只输出一个单词，不要有任何其他内容。"
)

# 合法意图集合
VALID_INTENTS = {"planner", "chat"}


def _get_client() -> OpenAI:
    """获取 DeepSeek 客户端（懒初始化，便于测试替换）"""
    return _client


def identify_intent(user_message: str) -> str:
    """识别用户意图

    Args:
        user_message: 用户消息文本

    Returns:
        "planner"（复习/排计划类） 或 "chat"（闲聊类）

    说明:
        调用 DeepSeek 做轻量分类；若调用失败或返回异常，
        降级为 "chat"（普通对话兜底，不影响主流程）。
    """
    if not user_message or not user_message.strip():
        return "chat"

    try:
        response = _client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": ROUTER_PROMPT},
                {"role": "user", "content": user_message},
            ],
            stream=False,
            max_tokens=8,  # 只需一个单词，节省 token
        )
        answer = (response.choices[0].message.content or "").strip().lower()
        # 兼容模型偶尔输出的多余内容（如 "planner." / "意图：planner"）
        for intent in VALID_INTENTS:
            if intent in answer:
                return intent
        return "chat"  # 无法识别时兜底为闲聊
    except Exception:
        # 意图识别失败时降级为普通对话，保证服务可用
        return "chat"
