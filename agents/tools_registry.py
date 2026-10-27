# agents/tools_registry.py
# A1 交付 1：工具注册表（白名单 + JSON Schema + handler + 描述）
# 规则不删：艾宾浩斯查表、课表工具原样复用，只是包成 Agent 可调用的工具

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from agents.kg import build_kg_context
from agents.prompts import EBBINGHAUS_PROMPT, SM2_PROMPT, build_tutor_feedback_prompt
from agents.storage import query_all
from tools.schedule import get_free_slots, get_free_time

BASE_DIR = Path(__file__).resolve().parent.parent
REVIEW_TASKS_PATH = BASE_DIR / "data" / "review_tasks.json"
SCHEDULE_JSON_PATH = BASE_DIR / "data" / "schedule.json"

load_dotenv()

# 懒初始化的 DeepSeek 客户端（供 generate_plan 这类「工具内部调用大模型」使用）
_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=os.getenv("DEEPSEEK_API_KEY"),
            base_url="https://api.deepseek.com",
        )
    return _client


# 周一~周日排序权重（排课表用）
WEEKDAY_ORDER = {"周一": 1, "周二": 2, "周三": 3, "周四": 4,
                 "周五": 5, "周六": 6, "周日": 7}

# 艾宾浩斯/SM-2 初始间隔表：默认 [1,2,4,7,15]（SM-2 初始序列）
# 薄弱科目可收紧为更密间隔；单词类科目高频、数学常作薄弱科目
REVIEW_INTERVALS = {
    "default": [1, 2, 4, 7, 15],
    "英语": [1, 2, 4, 7, 15, 30],
    "数学": [1, 2, 3, 5],       # 数学常为薄弱科目 -> 缩短间隔、加密复习
}


def _load_review_tasks() -> list:
    """读取复习任务（SQLite review_tasks 表；异常回退空列表）"""
    try:
        rows = query_all(
            "SELECT subject, chapter, due_date, priority, done FROM review_tasks"
        )
        return [
            {"subject": r["subject"], "chapter": r["chapter"],
             "due_date": r["due_date"], "priority": r["priority"],
             "done": bool(r["done"])}
            for r in rows
        ]
    except Exception:
        return []


# ---------------- 工具 handler（均无副作用，天然幂等） ----------------

def tool_query_due_reviews(args: dict) -> dict:
    """查询到期复习点（艾宾浩斯查表：due_date <= today 且未完成）"""
    days = int(args.get("days", 7))
    today = datetime.now().date()
    horizon = today + timedelta(days=days)
    tasks = _load_review_tasks()
    due = [
        t for t in tasks
        if not t.get("done")
        and datetime.strptime(t["due_date"], "%Y-%m-%d").date() <= horizon
    ]
    # 按到期日升序、优先级高的在前
    due.sort(key=lambda t: (t["due_date"], -int(t.get("priority", 1))))
    return {
        "success": True,
        "count": len(due),
        "due_reviews": due[:20],   # TopN，防 Prompt 塞爆
    }


def tool_compute_free_slots(args: dict, user_schedule=None) -> dict:
    """计算空闲时段（优先读用户动态课表 user_schedule；无则演示 JSON）"""
    min_minutes = int(args.get("min_minutes", 60))
    # user_schedule 为空列表 = 用户未配置课表（返回明确空态，前端走兜底）
    slots = get_free_slots(min_minutes=min_minutes, user_schedule=user_schedule)
    return {
        "success": True,
        "count": len(slots),
        "free_slots": slots,
        "summary": get_free_time(user_schedule=user_schedule),
        "note": (
            "用户未配置课表，无空闲时段数据。请勿输出具体时间表格，改用宏观学习建议。"
            if user_schedule is not None and len(slots) == 0 else ""
        ),
    }


def tool_get_student_profile(args: dict, profile: dict = None) -> dict:
    """查询学生画像（user_id 从调用上下文注入，禁止模型传）"""
    p = profile or {}
    return {
        "success": True,
        "profile": {
            "goal": p.get("goal", ""),
            "examDate": p.get("examDate", ""),
            "hours": p.get("hours", ""),
            "weak": p.get("weak", ""),
        },
    }


# ---------------- 新增①：用户课表工具 tool_get_schedule ----------------

def tool_get_schedule(args: dict, user_schedule=None) -> dict:
    """读取用户课表（工具函数，供大模型自主调用）

    数据源优先级：上下文注入的 user_schedule（前端「我的专属课表」录入）
                > data/schedule.json（本地 JSON 兜底）
    返回结构化课表 + 可直接嵌入 Prompt 的文本。
    """
    rows = []
    source = "用户专属课表（前端录入）"

    if user_schedule:
        for it in user_schedule:
            day = (it.get("day") or "").strip()
            start = (it.get("start") or "").strip()
            end = (it.get("end") or "").strip()
            course = (it.get("course") or "").strip()
            if day and start and end:
                rows.append({"day": day, "start": start, "end": end,
                             "course": course or "课程"})

    if not rows:   # 兜底：读取本地 JSON
        source = "本地 schedule.json"
        try:
            data = json.loads(SCHEDULE_JSON_PATH.read_text(encoding="utf-8"))
            for day, courses in (data.get("week_schedule") or {}).items():
                for c in courses:
                    rows.append({"day": day, "start": c.get("start", ""),
                                 "end": c.get("end", ""), "course": c.get("course", "")})
        except Exception:
            rows = []

    rows.sort(key=lambda r: (WEEKDAY_ORDER.get(r["day"], 9), r.get("start", "")))
    schedule_text = "；".join(
        f'{r["day"]} {r["start"]}-{r["end"]} {r["course"]}' for r in rows
    )
    return {
        "success": True,
        "count": len(rows),
        "source": source,
        "courses": rows,
        "schedule_text": schedule_text or "（用户未录入任何课程）",
        "note": "" if rows else "用户未配置课表：请勿输出具体时间表格，改用宏观学习建议。",
    }


# ---------------- 新增②：复习计划生成工具 tool_generate_plan ----------------

PLAN_TOOL_SYSTEM_PROMPT = (
    "你是「智学矩阵」复习计划生成器。请基于下方提供的课表、空闲时段、"
    "到期复习点与学生画像，生成一份可直接执行的复习计划。\n"
    "硬约束：\n"
    "1. 计划必须落在真实空闲时段内，绝对避开上课时间（精确到小时）。\n"
    "2. 若未提供课表/空闲时段，不要输出具体时间表格，改用宏观学习建议。\n"
    "3. 优先级：到期复习点 > 薄弱科目 > 常规进度；已逾期复习点优先靠前。\n"
    "4. 若用户指定了时间范围，只在该范围内排期，绝不超出。\n"
    "5. 用 Markdown 表格输出，列头固定为：| 日期 | 时间段 | 任务内容 |\n"
    "6. 结尾用一句话说明本次计划的决策依据。"
) + "\n\n" + EBBINGHAUS_PROMPT + "\n\n" + SM2_PROMPT


def tool_generate_plan(args: dict, context: dict = None) -> dict:
    """结合课表 + 用户输入，调用大模型生成复习计划（工具函数）

    args:    {"weak_subject": str?, "time_limit": str?}
    context: 服务端注入（profile / user_schedule / user_message / schedule_text / progress_data）
    """
    ctx = context or {}
    profile = ctx.get("profile") or {}
    weak_subject = (args.get("weak_subject") or profile.get("weak") or "").strip()
    time_limit = (args.get("time_limit") or "").strip()
    try:
        recall_score = args.get("recall_score")
        recall_score = int(recall_score) if recall_score is not None else None
    except (TypeError, ValueError):
        recall_score = None
    user_message = ctx.get("user_message") or "请生成复习计划"

    # 内部复用既有工具，收集计划所需上下文（课表 / 空闲 / 到期复习点）
    schedule = tool_get_schedule({}, user_schedule=ctx.get("user_schedule"))
    free = tool_compute_free_slots({}, user_schedule=ctx.get("user_schedule"))
    due = tool_query_due_reviews({"days": 7})

    now = datetime.now()
    week = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][now.weekday()]
    sys_prompt = PLAN_TOOL_SYSTEM_PROMPT
    sys_prompt += (
        f"\n\n【系统时间】{now.year}年{now.month}月{now.day}日 {week} "
        f"{now.hour:02d}:{now.minute:02d}"
    )
    if weak_subject:
        sys_prompt += f"\n【薄弱科目】{weak_subject}（优先安排）"
    if time_limit:
        sys_prompt += f"\n【用户指定时间范围】{time_limit}（只在此范围内排期）"
    if profile:
        sys_prompt += (
            f"\n【学生画像】目标={profile.get('goal', '')}"
            f"；考试日期={profile.get('examDate', '')}；每日可用={profile.get('hours', '')}"
        )
    if ctx.get("progress_data"):
        sys_prompt += f"\n【打卡进度】{ctx['progress_data']}"
    if recall_score is not None:
        sys_prompt += (
            f"\n【用户记忆自评】{recall_score} / 5 分 → 按 SM-2 动态调整下一次复习间隔"
            "（≥4 分 ×1.5 延后；≤2 分 ×0.5 提前并强制重刺；=3 分保持不变），"
            "并在计划表备注中说明「基于您的记忆自评，为您动态调整了复习间隔」。"
        )
    # ★ 督学反馈：基于本周计划完成率（<50% 批评+番茄钟；>80% 表扬+加量）
    feedback = build_tutor_feedback_prompt(
        done=ctx.get("completion_done"), total=ctx.get("completion_total"),
        rate=ctx.get("completion_rate"),
    )
    if feedback:
        sys_prompt += "\n\n" + feedback
    sys_prompt += f"\n【用户课表】{schedule['schedule_text']}"
    sys_prompt += f"\n【空闲时段】{json.dumps(free.get('free_slots', []), ensure_ascii=False)}"
    sys_prompt += f"\n【到期复习点】{json.dumps(due.get('due_reviews', []), ensure_ascii=False)}"

    try:
        resp = _get_client().chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_message},
            ],
            stream=False,
        )
        plan = resp.choices[0].message.content or ""
        return {
            "success": True,
            "weak_subject": weak_subject,
            "time_limit": time_limit,
            "recall_score": recall_score,
            "plan": plan,
        }
    except Exception as e:
        return {"success": False, "error": f"生成计划失败: {e}", "retryable": True}


# ---------------- 新增③：出题工具 tool_generate_test（Tutor Agent） ----------------

QUIZ_TOOL_SYSTEM_PROMPT = (
    "你是一个出题专家（Tutor Agent）。请根据用户选择的科目出一道针对性的单选题。\n"
    "要求：\n"
    "1. 题目紧扣该科目知识点，难度适中，题干清晰；\n"
    "2. 只出单选题：必须且只能有 A/B/C/D 四个选项，三个干扰项要贴合常见错误思路、具备迷惑性；\n"
    "3. 只出题，不要在题干中泄露答案；\n"
    "4. 【输出格式 · 强约束】必须严格按以下纯文本 Markdown 格式输出，"
    "不得使用表格、不得增删标记行：\n"
    "【题目】这里是题干\n"
    "A. 选项内容\n"
    "B. 选项内容\n"
    "C. 选项内容\n"
    "D. 选项内容\n"
    "【答案】A\n"
    "【解析】这里是解析\n"
    "   —— 【答案】后只写一个大写字母（A/B/C/D）；【解析】须说明正确项为何正确、其余三项错在哪；\n"
    "5. 【知识图谱 · 强约束】请基于知识图谱，出一道「关联前置知识点」的综合应用题：\n"
    "   题目必须同时考查【当前知识点】与【图谱中给出的前置知识点】中的至少一个；\n"
    "   但仍须使用上面的统一选择题格式，关联关系写在【解析】里，不要再输出格式之外的任何说明句。"
)


def tool_generate_test(args: dict, context: dict = None) -> dict:
    """出题工具：调用大模型生成测试题/练习题

    args:    {"subject": str?, "count": int?, "difficulty": str?}
    context: 服务端注入（profile / progress_data / user_message）
    """
    ctx = context or {}
    profile = ctx.get("profile") or {}
    subject = (args.get("subject") or "").strip() or "综合"
    try:
        count = int(args.get("count") or 1)
    except (TypeError, ValueError):
        count = 1
    count = max(1, min(count, 10))
    difficulty = (args.get("difficulty") or "").strip()

    # ★ 知识图谱：定位当前知识点 + 回溯前置知识点（跨节点出题依据）
    topic_text = (args.get("topic") or "").strip() or (ctx.get("user_message") or "")
    kg = build_kg_context(subject, topic_text, depth=2)

    sys_prompt = QUIZ_TOOL_SYSTEM_PROMPT
    sys_prompt += f"\n\n【科目】{subject}\n【题目数量】{count}"
    if topic_text:
        sys_prompt += f"\n【用户意图 / 知识点线索】{topic_text[:80]}"
    if difficulty:
        sys_prompt += f"\n【难度】{difficulty}"
    weak = (profile.get("weak") or "").strip()
    if weak:
        sys_prompt += f"\n【用户薄弱点】{weak}（出题请重点覆盖）"
    if ctx.get("progress_data"):
        sys_prompt += f"\n【用户打卡进度】{ctx['progress_data']}"
    if kg.get("text"):
        sys_prompt += "\n\n" + kg["text"]                    # ★ 图谱上下文注入

    try:
        resp = _get_client().chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user",
                 "content": ctx.get("user_message") or f"请针对「{subject}」出一道题考考我。"},
            ],
            stream=False,
        )
        quiz = resp.choices[0].message.content or ""
        return {"success": True, "subject": subject, "count": count,
                "topic": (kg.get("topic") or {}).get("name", ""),
                "prerequisites": [p["name"] for p in kg.get("prerequisites", [])],
                "quiz": quiz}
    except Exception as e:
        return {"success": False, "error": f"出题失败: {e}", "retryable": True}


# ---------------- 新增④：知识图谱查询工具 tool_query_knowledge_graph ----------------

def tool_query_knowledge_graph(args: dict, context: dict = None) -> dict:
    """查询知识图谱：给定科目/知识点，返回前置知识点与关联节点

    args: {"subject": str, "topic": str?, "depth": int?}
    """
    subject = (args.get("subject") or "").strip()
    topic = (args.get("topic") or "").strip()
    try:
        depth = int(args.get("depth") or 2)
    except (TypeError, ValueError):
        depth = 2

    kg = build_kg_context(subject, topic, depth=depth)
    node = kg.get("topic") or {}
    return {
        "success": True,
        "subject": kg.get("subject"),
        "topic": node.get("name", ""),
        "chapter": node.get("chapter", ""),
        "prerequisites": [p["name"] for p in kg.get("prerequisites", [])],
        "successors": [s["name"] for s in kg.get("successors", [])],
        "kg_text": kg.get("text", ""),
    }


# ---------------- 工具注册表（白名单） ----------------
# name / description / parameters(JSON Schema) / handler / needs_profile
TOOL_REGISTRY = {
    "get_schedule": {
        "description": (
            "查询用户课表（get_schedule）。读取用户在前端「我的专属课表」录入的课程"
            "（无则读本地 schedule.json）。当用户想看/查询课表、问哪天有课、"
            "问什么时候有空时调用。无需参数。"
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
        "handler": tool_get_schedule,
        "needs_user_schedule": True,   # 从调用上下文注入用户课表行
    },
    "generate_test": {
        "description": (
            "出题/生成测试题（generate_test，Tutor Agent）。"
            "当用户要求出题、做练习、模拟测试、考考我、刷题时调用。"
            "会基于知识图谱出「关联前置知识点」的综合应用题。"
            "subject=科目（可选）；topic=知识点（可选，如 级数/二叉树遍历）；"
            "count=题目数量 1-10（可选，默认 1）；difficulty=难度（可选，如 简单/中等/困难）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "topic": {"type": "string"},
                "count": {"type": "integer", "minimum": 1, "maximum": 10},
                "difficulty": {"type": "string"},
            },
            "required": [],
        },
        "handler": tool_generate_test,
        "needs_context": True,         # 需要 profile / 进度 / 用户消息
    },
    "query_knowledge_graph": {
        "description": (
            "查询知识图谱（Knowledge Graph）：给定科目与知识点，"
            "返回其前置知识点与关联节点（跨节点出题 / 讲解前后置依赖用）。"
            "subject=科目（必填）；topic=知识点（可选）；depth=前置层数 1-3（可选，默认 2）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "topic": {"type": "string"},
                "depth": {"type": "integer", "minimum": 1, "maximum": 3},
            },
            "required": ["subject"],
        },
        "handler": tool_query_knowledge_graph,
    },
    "generate_plan": {
        "description": (
            "生成复习计划。结合用户课表与用户输入，调用大模型产出可执行计划。"
            "当用户要求制定/安排复习计划时调用。"
            "weak_subject=薄弱科目（可选，缺省用画像中的薄弱科目）；"
            "time_limit=时间范围（可选，如“明天下午”“本周”）；"
            "recall_score=用户上次复习的记忆自评 0-5（可选；≥4 分间隔×1.5，≤2 分×0.5 并强制重刺）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "weak_subject": {"type": "string"},
                "time_limit": {"type": "string"},
                "recall_score": {"type": "integer", "minimum": 0, "maximum": 5},
            },
            "required": [],
        },
        "handler": tool_generate_plan,
        "needs_context": True,   # handler 需要完整调用上下文（profile/schedule/message）
    },
    "query_due_reviews": {
        "description": "查询到期复习点（含艾宾浩斯间隔调度）。days=查询未来几天，默认7。",
        "parameters": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": 30},
            },
            "required": [],
        },
        "handler": tool_query_due_reviews,
        "needs_profile": False,
    },
    "compute_free_slots": {
        "description": "计算本周空闲时段（读取用户录入的专属课表；未配置时返回空并提示改用宏观建议）。min_minutes=最小时段（分钟），默认60。",
        "parameters": {
            "type": "object",
            "properties": {
                "min_minutes": {"type": "integer", "minimum": 15, "maximum": 480},
            },
            "required": [],
        },
        "handler": tool_compute_free_slots,
        "needs_profile": False,
        "needs_user_schedule": True,   # 从调用上下文注入用户课表行
    },
    "get_student_profile": {
        "description": "查询学生画像：学习目标、考试日期、每日可用时长、薄弱科目。",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "handler": tool_get_student_profile,
        "needs_profile": True,   # handler 需要调用上下文注入 profile
    },
}


def get_tool_schemas() -> list:
    """生成 DeepSeek Function Calling 所需的 tools 参数"""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": spec["description"],
                "parameters": spec["parameters"],
            },
        }
        for name, spec in TOOL_REGISTRY.items()
    ]
