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
from agents.storage import execute, now_str, query_all
from agents import review as review_engine
from agents import plan_store
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


# ---------------- 遗忘刺客核心工具（SM-2 落库版，P0-1） ----------------

def tool_register_knowledge_point(args: dict, context: dict = None) -> dict:
    """登记知识点 → 生成 5 档艾宾浩斯复习计划（第 1 天必刺）

    args: {"subject": str, "content": str, "first_learn_date": "YYYY-MM-DD"?}
    """
    ctx = context or {}
    subject = (args.get("subject") or "").strip()
    content = (args.get("content") or "").strip()
    if not content:
        return {"success": False, "error": "content（知识点内容）不能为空", "retryable": True}
    if not subject:
        subject = (ctx.get("profile") or {}).get("weak") or "综合"
    rec = review_engine.register_kp(
        ctx.get("user_id") or "default", subject, content,
        first_learn_date=(args.get("first_learn_date") or ""),
    )
    if "error" in rec:
        return {"success": False, "error": rec["error"], "retryable": True}
    return {
        "success": True,
        "kp_id": rec["kp_id"], "subject": rec["subject"], "content": rec["content"],
        "next_review_date": rec["next_review_date"],
        "review_dates": rec["review_dates"],
        "intervals": rec["intervals"],
        "note": (f"已登记知识点并生成 {len(rec['review_dates'])} 档艾宾浩斯复习计划，"
                 f"下次出刺日 {rec['next_review_date']}。请在回复中告知用户复习日期序列。"),
    }


def tool_submit_recall_score(args: dict, context: dict = None) -> dict:
    """提交记忆自评（1~5）→ SM-2 真实算法动态调整下次出刺日期

    args: {"subject": str, "content": str, "memory_score": int}
    """
    ctx = context or {}
    subject = (args.get("subject") or "").strip()
    content = (args.get("content") or "").strip()
    score = args.get("memory_score")
    if not content:
        return {"success": False, "error": "content（知识点内容）不能为空", "retryable": True}
    try:
        score = int(score)
    except (TypeError, ValueError):
        return {"success": False, "error": "memory_score 必须是 1~5 整数", "retryable": True}
    if not 1 <= score <= 5:
        return {"success": False, "error": "memory_score 必须是 1~5 整数", "retryable": True}
    result = review_engine.submit_score(
        ctx.get("user_id") or "default", subject, content, score)
    if "error" in result:
        return {"success": False, "error": result["error"],
                "retryable": True,
                "hint": "可提示用户先用自然语言登记该知识点，或换更精确的表述重试。"}
    return {
        "success": True,
        "kp_id": result["kp_id"], "subject": subject, "content": content,
        "stage_before": result["stage_before"], "stage_after": result["stage_after"],
        "interval_before": result["interval_before"],
        "interval_after": result["interval_after"],
        "next_review_date": result["next_review_date"],
        "restrike": result["restrike"], "back_to_stage1": result["back_to_stage1"],
        "completed": result["completed"],
        "note": result["message"],
    }


def tool_daily_targets(args: dict, context: dict = None) -> dict:
    """今日必刺清单（knowledge_point 表 SQL 硬约束，优先级最高）

    args: {"days_ahead": int?（默认 0 = 截至今天）}
    """
    ctx = context or {}
    try:
        days_ahead = int(args.get("days_ahead") or 0)
    except (TypeError, ValueError):
        days_ahead = 0
    targets = review_engine.daily_targets(ctx.get("user_id") or "default", days_ahead)
    return {
        "success": True,
        "count": len(targets),
        "targets": [
            {"subject": t["subject"], "content": t["content"],
             "next_review_date": t["next_review_date"],
             "review_stage": t["review_stage"]}
            for t in targets[:20]
        ],
        "note": ("这些是到期必须复习的知识点（今日必刺清单），生成计划时必须排入且优先靠前。"
                 if targets else "当前没有到期的复习知识点。"),
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
    "你是「遗忘刺客」复习计划生成器（产品名：智学矩阵）。请基于下方提供的课表、空闲时段、"
    "到期复习点与学生画像，生成一份可直接执行的复习计划。\n"
    "硬约束：\n"
    "1. 计划必须落在真实空闲时段内，绝对避开上课时间（精确到小时）。\n"
    "2. 若未提供课表/空闲时段，不要输出具体时间表格，改用宏观学习建议。\n"
    "3. 优先级：到期复习点 > 薄弱科目 > 常规进度；已逾期复习点优先靠前。\n"
    "4. 若用户指定了时间范围，只在该范围内排期，绝不超出。\n"
    "5. 用 Markdown 表格输出，列头固定为：| 日期 | 时间段 | 任务内容 |\n"
    "6. 结尾用一句话说明本次计划的决策依据。\n"
    "7. 【输出前自检 · 强制】输出前逐条核对：每条任务时间落在空闲时段内且不与上课冲突；"
    "到期复习点排在最前未被挤占；单次任务 25~90 分钟、每日总量不超画像可用时长；"
    "表格严格三列。任一条不满足必须修正后重排，再输出。"
) + "\n\n" + EBBINGHAUS_PROMPT + "\n\n" + SM2_PROMPT


def _parse_exam_date(s):
    """解析考试日期字符串 -> datetime.date（支持 2026-11-15 / 2026/11/15 / 2026年11月15日）；无效返回 None"""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            continue
    return None


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
        if recall_score is None:
            recall_score = ctx.get("recall_score")   # 兜底：服务端正则解析的自评分
        recall_score = int(recall_score) if recall_score is not None else None
    except (TypeError, ValueError):
        recall_score = None
    user_message = ctx.get("user_message") or "请生成复习计划"

    # 内部复用既有工具，收集计划所需上下文（课表 / 空闲 / 到期复习点）
    schedule = tool_get_schedule({}, user_schedule=ctx.get("user_schedule"))
    free = tool_compute_free_slots({}, user_schedule=ctx.get("user_schedule"))
    due = tool_query_due_reviews({"days": 7})
    # ★ 今日必刺清单（knowledge_point 表，SM-2 落库版；优先级最高）
    today_targets = tool_daily_targets({"days_ahead": 0}, context=ctx)

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
        # 考试倒计时（画像首个有效考试日期）
        exam_date = _parse_exam_date(profile.get("examDate", ""))
        if exam_date is not None:
            days_left = (exam_date - now.date()).days
            sys_prompt += (
                f"\n【考试倒计时】距 {exam_date.isoformat()} 还有 {days_left} 天，"
                "请按剩余天数倒推各科目复习进度，优先覆盖考试科目。"
            )
        # 画像科目清单（要求计划不偏离用户所学科目）
        subject_names = [s.get("subject") or "" for s in (profile.get("subjects") or [])]
        subject_names = [s.strip() for s in subject_names if s and s.strip()]
        if subject_names:
            sys_prompt += (
                "\n【用户所学科目】" + "、".join(subject_names[:8])
                + "。计划任务原则上只从这些科目中安排（到期复习点与薄弱科目优先），"
                "不要引入用户未学习的科目。"
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
    # ★ 今日必刺清单（优先级高于一切，必须排入且靠前）
    if today_targets.get("count"):
        sys_prompt += (
            "\n【今日必刺清单 · 最高优先级（必须排入计划且排在最前，不可挤占）】"
            + json.dumps(today_targets.get("targets", []), ensure_ascii=False)
        )

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
        # ★ P0-2：计划结构化落库（Markdown → plan_item 表，打卡/完成率走 SQL）
        stored = plan_store.store_plan(ctx.get("user_id") or "default", plan)
        return {
            "success": True,
            "weak_subject": weak_subject,
            "time_limit": time_limit,
            "recall_score": recall_score,
            "plan": plan,
            "plan_items_stored": stored.get("count", 0),
        }
    except Exception as e:
        return {"success": False, "error": f"生成计划失败: {e}", "retryable": True}


# ---------------- 新增③：出题工具 tool_generate_test（Tutor Agent） ----------------

QUIZ_TOOL_SYSTEM_PROMPT = (
    "你是「遗忘刺客」出题专家（Tutor Agent，产品名：智学矩阵）。请根据用户选择的科目出一道针对性的题目。\n"
    "要求：\n"
    "1. 题目紧扣该科目知识点，难度适中，题干清晰；\n"
    "2. 选择题给出 4 个选项（A/B/C/D）；简答题给出答题要点；\n"
    "3. 先只出题，不要立刻给答案；在题目下方附一行「（作答后回复你的答案，我会点评）」；\n"
    "4. 若用户已作答，请点评：指出对错、说明考点、给出改进建议；\n"
    "5. 【知识图谱 · 强约束】请基于知识图谱，出一道「关联前置知识点」的综合应用题：\n"
    "   题目必须同时考查【当前知识点】与【图谱中给出的前置知识点】中的至少一个，体现跨节点综合能力；\n"
    "   出题后必须注明一行「本题关联前置知识点：<前置知识点>」，并简述二者如何串联在一道题里；\n"
    "6. 【考点锚点】若下方提供了【当前学习计划涉及的科目/知识点】，题目必须与之直接相关——"
    "优先考当前复习计划中的科目与知识点，不考与计划无关的内容；\n"
    "7. 【判题依据】出题后在末尾附一行「（考点：<本题考查的具体知识点>；答题要点：<2~3 条要点>）」，"
    "便于用户作答后对照自评，也便于 AI 点评时对错判定。"
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
    # ★ 考点锚点：当前学习计划涉及的科目（画像科目 + 最近历史科目），出题不偏离计划
    plan_subjects = [s.get("subject") or "" for s in (profile.get("subjects") or [])]
    plan_subjects = [s.strip() for s in plan_subjects if s and s.strip()]
    try:
        from agents.history import load_history
        for r in load_history(ctx.get("user_id") or None, limit=3):
            for s in (r.get("subjects") or []):
                s = (s or "").strip()
                if s and s not in plan_subjects:
                    plan_subjects.append(s)
    except Exception:
        pass
    if plan_subjects:
        sys_prompt += (
            "\n【当前学习计划涉及的科目】" + "、".join(plan_subjects[:6])
            + "。出题请优先从中选择考点，保证题目与用户当前复习计划直接相关。"
        )
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
        # ★ 出题-判题闭环：题目落库 quiz_log（后续 /api/quiz/answer 判分回写）
        try:
            quiz_id = execute(
                "INSERT INTO quiz_log(user_id, subject, topic, question, answer, "
                "correct, kp_id, created_at) VALUES (?,?,?,?,NULL,NULL,NULL,?)",
                (ctx.get("user_id") or "default", subject,
                 (kg.get("topic") or {}).get("name", ""), quiz[:4000], now_str()),
            )
        except Exception:
            quiz_id = None
        return {"success": True, "subject": subject, "count": count,
                "topic": (kg.get("topic") or {}).get("name", ""),
                "prerequisites": [p["name"] for p in kg.get("prerequisites", [])],
                "quiz": quiz, "quiz_id": quiz_id}
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
    # ---------------- 遗忘刺客核心工具（SM-2 落库版） ----------------
    "register_knowledge_point": {
        "description": (
            "登记知识点并生成艾宾浩斯复习计划（复习 Agent 出刺）。"
            "当用户说「我刚学了/今天学完了 XX」「帮我记住这个知识点」「安排复习」时调用。"
            "登记后会按第 1/2/4/7/15 天生成 5 档复习点并落库，返回复习日期序列。"
            "subject=科目（如 高数，可选，缺省用薄弱科目）；"
            "content=知识点内容（必填，如 级数敛散性判别）；"
            "first_learn_date=首次学习日期 YYYY-MM-DD（可选，默认今天）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "content": {"type": "string"},
                "first_learn_date": {"type": "string"},
            },
            "required": ["content"],
        },
        "handler": tool_register_knowledge_point,
        "needs_context": True,
    },
    "submit_recall_score": {
        "description": (
            "提交某知识点的记忆自评（1~5 分），由 SM-2 算法动态调整下次复习日期并落库。"
            "当用户复习完某知识点并给出自评（如「级数我记住了，自评 4 分」）时调用。"
            "≥4 分间隔 ×1.5 延后；≤2 分 ×0.5 提前并强制重刺；3 分不变。"
            "subject=科目（可选）；content=知识点内容（必填，须与登记时一致）；"
            "memory_score=自评 1~5（必填）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "content": {"type": "string"},
                "memory_score": {"type": "integer", "minimum": 1, "maximum": 5},
            },
            "required": ["content", "memory_score"],
        },
        "handler": tool_submit_recall_score,
        "needs_context": True,
    },
    "get_today_review_targets": {
        "description": (
            "获取今日必刺清单（到期的艾宾浩斯复习知识点，来自 knowledge_point 表）。"
            "当用户问「今天要复习什么」「我有什么到期的知识点」时调用；"
            "生成复习计划前也必须先调用本工具，把到期知识点排入计划最前。"
            "days_ahead=往前看几天（可选，默认 0 = 截至今天）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days_ahead": {"type": "integer", "minimum": 0, "maximum": 30},
            },
            "required": [],
        },
        "handler": tool_daily_targets,
        "needs_context": True,
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
