"""StudyMatrix - 智学矩阵

基于 Flask 的 Web 服务入口：
- 开启 CORS 跨域支持
- 静态文件目录指向 static/
- /api/chat：多 Agent 调度（SSE 流式推送过程状态）
    1. Router Agent（agents/router.py）识别意图            -> event: router
    2. planner 时调用课表工具 tools/schedule.get_free_time -> event: tool
    3. DeepSeek 生成最终回复                                -> event: final
"""

import json
import os
import re
import time
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_from_directory
from flask_cors import CORS
from openai import OpenAI

from agents.history import append_record, build_history_prompt, extract_subjects, load_history
from agents.kg import build_kg_context, list_subjects as kg_list_subjects
from agents.loop import run_agent
from agents.memory import build_memory_prompt, load_recent_memory, save_memory
from agents.planner import make_plan
from agents import plan_store
from agents import review as review_engine
from agents.prompts import (build_tutor_feedback_prompt,
                            TUTOR_LOW_THRESHOLD, TUTOR_HIGH_THRESHOLD)
from agents.storage import init_db, query_one
from agents.tools_registry import tool_query_due_reviews

init_db()   # 初始化 SQLite（data/app.db）表结构，幂等
# 注：agents/router.py 的写死意图分类（identify_intent）已退役——
# 路由改由大模型通过 Function Calling 自主决策（见 agents/loop.py）

# 加载 .env 环境变量
load_dotenv()

# DeepSeek API Key（从 .env 读取）
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

# 中文星期映射（datetime.weekday(): 0=周一 ... 6=周日）
WEEKDAY_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def get_now_str() -> str:
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
    缺失字段自动跳过，全部缺失时返回空串。
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


def build_schedule_prompt(schedule_text: str) -> str:
    """用户专属课表 → 强约束提示词块（动态列表录入，精确到小时）

    有课表：要求精确排课（精确到时间段）
    无课表：强约束宏观建议（禁止具体时间表格）——兜底策略
    """
    s = (schedule_text or "").strip()
    if not s:
        # 兜底：用户未提供课表
        return (
            "【课表兜底 · 强约束】当前用户未提供课表。"
            "请不要输出任何包含具体时间（如 8:00-10:00）的详细表格。"
            "请以文字列表的形式，提供宏观的复习策略和学习方法建议。"
        )
    return (
        "【用户专属课表 · 强约束】当前用户的真实课表如下：" + s + "\n"
        "排课规则：请严格结合该课表，给出精确到时间段的详细计划，"
        "绝对避开上课时间（精确到小时），只在空闲时间里见缝插针地安排复习任务。"
    )


# ---------- Router 增强：意图细分检测 ----------


def detect_focused_time(message: str) -> bool:
    """检测用户提问是否包含明确的时间限定词（精准定位场景）

    命中时模型只回答该时间段的安排，拒绝输出整周大表格。
    """
    focused_words = [
        "今天", "明天", "后天", "今晚", "今早", "下午", "上午", "晚上",
        "接下来", "接下来两小时", "接下来一小时", "这周末", "本周周末",
        "这周", "现在开始", "接下来半天",
    ]
    return any(w in message for w in focused_words)


def detect_progress_query(message: str) -> bool:
    """检测用户是否在询问学习进度/完成情况（监督员场景）"""
    progress_words = [
        "进度", "完成率", "完成情况", "学了多少", "学了多少",
        "打卡", "做了多少", "任务完成", "我的进展", "进展如何",
        "完成得怎么样", "做得怎么样",
    ]
    return any(w in message for w in progress_words)


def detect_recall_score(message: str):
    """从用户消息中解析「记忆自评」得分（0-5）。返回 int 或 None（SM-2 动态间隔用）

    支持写法：自评 4 分 / 记忆自评：4/5 / 评分 2 / 3 分（自动排除“满分”误判）
    """
    if not message:
        return None
    patterns = [
        r"记忆自评[^\d]{0,6}(\d(?:\.\d)?)",
        r"自评[^\d]{0,4}(\d(?:\.\d)?)",
        r"评分[^\d]{0,4}(\d(?:\.\d)?)",
        r"(\d(?:\.\d)?)\s*/\s*5",
        r"(\d(?:\.\d)?)\s*分",
    ]
    for p in patterns:
        for m in re.finditer(p, message):
            ctx_head = message[max(0, m.start() - 2):m.start()]
            if "满分" in ctx_head or "总分" in ctx_head:
                continue
            try:
                v = float(m.group(1))
            except (TypeError, ValueError):
                continue
            if 0 <= v <= 5:
                return int(round(v))
    return None


def parse_completion(data: dict):
    """从请求体解析「本周计划完成率」→ (done, total, rate)

    支持：completion_done/completion_total/completion_rate，或 completion="2/10"
    """
    done, total, rate = data.get("completion_done"), data.get("completion_total"), data.get("completion_rate")
    text = (data.get("completion") or "").strip()
    try:
        done = int(done) if done is not None else None
    except (TypeError, ValueError):
        done = None
    try:
        total = int(total) if total is not None else None
    except (TypeError, ValueError):
        total = None
    if (done is None or total is None) and "/" in text:
        try:
            a, b = text.split("/", 1)
            done, total = int(a.strip()), int(b.strip())
        except (TypeError, ValueError):
            pass
    try:
        rate = float(rate) if rate is not None else None
    except (TypeError, ValueError):
        rate = None
    if rate is None and isinstance(done, int) and isinstance(total, int) and total > 0:
        rate = done / total
    return done, total, rate


def tutor_mode_of(rate):
    """完成率 → 督学模式（供 SSE 展示）：harsh / praise / normal / ''"""
    if rate is None:
        return ""
    if rate < TUTOR_LOW_THRESHOLD:
        return "harsh"
    if rate > TUTOR_HIGH_THRESHOLD:
        return "praise"
    return "normal"


# ---------- 任务5：出题专家强约束 ----------

def build_quiz_prompt(subject: str, weak: str = "", progress_data: str = "") -> str:
    """出题测试 → 出题专家强约束提示词"""
    subject = (subject or "综合").strip()
    prompt = (
        f"你是一个出题专家。请根据用户选择的科目：{subject}，"
        "结合用户当前的薄弱点，出一道针对性的选择题或简答题。\n"
        "要求：\n"
        "1. 题目必须紧扣该科目知识点，难度适中，题干清晰\n"
        "2. 选择题给出 4 个选项（A/B/C/D）；简答题给出答题要点\n"
        "3. 先只出题，不要立刻给答案；在题目下方附一行「（作答后回复你的答案，我会点评）」\n"
        "4. 如果用户回答后，请给予点评：指出对错、说明考点、给出改进建议"
    )
    if weak:
        prompt += f"\n5. 用户自述的薄弱点：{weak}，出题时请重点覆盖"
    if progress_data:
        prompt += f"\n6. 用户打卡进度：{progress_data}（可参考其学习节奏）"
    return prompt


def build_plan_slots_prompt(plan_slots: list) -> str:
    """用户勾选的未来三日时段 → 强约束提示词块（不超范围排期）"""
    if not plan_slots:
        return ""
    slots = [str(s).strip() for s in plan_slots if str(s).strip()]
    if not slots:
        return ""
    return (
        "【用户指定时段 · 强约束】用户只希望在以下具体时段安排复习："
        + "、".join(slots)
        + "。请严格只针对这些时段生成计划（每个时段 1 条任务即可，最多 3 条），"
        "绝对不要超出这些时段排期，也不要输出整周或更长时间跨度的表格。"
        "若某时段与用户课表冲突，请跳过并简短说明。"
    )


def format_schedule_reply(schedule_rows: list) -> str:
    """把用户课表行格式化为课表信息文本（查课表分支专用，不含复习计划）"""
    if not schedule_rows:
        return (
            "你还没有录入课表哦。\n\n"
            "请点击左侧 **「我的专属课表」** 录入你的上课时间，"
            "录入后我就能随时为你查询课表安排。"
        )
    days_order = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    groups = {}
    for r in schedule_rows:
        d = (r.get("day") or "").strip()
        if not d:
            continue
        groups.setdefault(d, []).append(r)
    if not groups:
        return "你还没有录入课表哦。请点击左侧「我的专属课表」录入。"

    lines = ["你本周的课表安排如下：", ""]
    lines.append("| 日期 | 时间段 | 课程 |")
    lines.append("|---|---|---|")
    for d in days_order:
        for r in sorted(groups.get(d, []), key=lambda x: x.get("start", "")):
            lines.append(
                f"| {d} | {r.get('start','')}-{r.get('end','')} | {r.get('course','')} |"
            )
    total = sum(len(v) for v in groups.values())
    lines.append("")
    lines.append(f"共 {total} 门课程。如需查看某天的空闲时间，直接告诉我即可。")
    return "\n".join(lines)


# 闲聊 Agent 系统提示词：智能学习助手人设（调用时动态注入当前时间）
CHAT_PROMPT = (
    "你是「智学矩阵 StudyMatrix」的 AI 学习助手，风格简洁友好。"
    "你擅长：制定复习计划、查询空闲课表、模拟出题测试。"
    "回复请使用 Markdown 格式，列表或表格优先，方便前端渲染。"
)

app = Flask(__name__, static_folder="static", static_url_path="/static")

# 开启 CORS 跨域支持
CORS(app)

# DeepSeek 客户端（openai 库兼容接口，指向 DeepSeek 服务）
client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")


@app.route("/")
def index():
    """返回前端首页"""
    return send_from_directory(app.static_folder, "index.html")


@app.route("/api/health")
def health():
    """健康检查接口"""
    return jsonify({"status": "ok", "project": "StudyMatrix"})


def _sse(event: str, data: dict) -> str:
    """构造一条 SSE 消息"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@app.route("/api/chat", methods=["POST"])
def chat():
    """AI 对话接口（SSE 流式）：Router 识别意图 -> 工具调用 -> 最终回复

    请求体: {"message": "用户消息文本"}
    响应（text/event-stream）事件序列:
        event: router  data: {"intent": "chat|planner", "agent": "..."}
        event: tool    data: {"status": "loading", "message": "..."}     （仅 planner）
        event: final   data: {"reply": "...", "intent": "...", "agent": "...", "tool": "done"}
        event: error   data: {"error": "..."}   （异常时）
    """
    # 注意：请求体必须在视图函数内先解析好，
    # 生成器执行时已脱离请求上下文，不能再访问 request
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    profile = data.get("profile") or {}   # 学习画像：{goal, subjects, hours, weak}
    schedule_text = (data.get("schedule") or "").strip()   # 用户专属课表文本
    progress_data = (data.get("progress_data") or "").strip()   # 打卡进度文本
    schedule_rows = data.get("schedule_rows") or []   # 课表行数组（工具上下文注入用）
    if not isinstance(schedule_rows, list):
        schedule_rows = []
    plan_slots = data.get("plan_slots") or []   # 规划弹窗勾选的时段（未来三日）
    if not isinstance(plan_slots, list):
        plan_slots = []
    user_id = (data.get("user_id") or "").strip() or "default"   # Memory：用户标识

    # ---------- Router 细分意图检测（注：intent 由 generate 内 identify_intent 判定，
    # 此处先做时间限定词与进度查询的独立标记，供提示词分支使用） ----------
    is_focused_time = detect_focused_time(message)
    is_progress_query = detect_progress_query(message)
    recall_score = detect_recall_score(message)   # ★ SM-2：记忆自评（0-5 或 None）
    completion_done, completion_total, completion_rate = parse_completion(data)   # ★ 督学反馈：完成率

    def generate():
        if not message:
            yield _sse("error", {"error": "消息不能为空"})
            return

        if not DEEPSEEK_API_KEY:
            yield _sse("error", {"error": "服务端未配置 DEEPSEEK_API_KEY"})
            return

        # ---------- 1. Router Agent：不再写死 if-else ----------
        # 由大模型通过 OpenAI 兼容的 Function Calling 自主决策调用哪个工具：
        #   用户问课表 → 模型决定调用 get_schedule
        #   用户要出题 → 模型决定调用 generate_test
        #   用户要排计划 → 模型决定调用 generate_plan
        intent = "auto"
        agent = "Agent（大模型自主决策工具）"
        yield _sse("router", {
            "route": "auto",
            "agent": agent,
            "message": "Agent 正在自主决策：是否需要调用工具",
            "completion": (f"{int(round(completion_rate * 100))}%" if completion_rate is not None else ""),
            "tutor_mode": tutor_mode_of(completion_rate),
        })

        # ---------- 2. 统一 Agent Loop：工具选择完全交给大模型 ----------
        yield _sse("tool", {
            "status": "loading",
            "message": "Agent 正在感知学习环境（课表/复习点/画像）...",
        })

        # 组装 Agent 追加约束：进度感知 + 用户指定时段 + 历史记忆 + 精准定位/监督员
        agent_extra = ""
        if progress_data:
            agent_extra += (
                f"【用户打卡进度】：{progress_data}"
                "排新计划时可参考该进度，避免重复安排已完成的任务。\n\n"
            )
        slots_prompt = build_plan_slots_prompt(plan_slots)
        if slots_prompt:
            agent_extra += slots_prompt + "\n\n"
        # ---------- SM-2 动态间隔：用户记忆自评（0-5）→ 调整复习间隔 ----------
        if recall_score is not None:
            agent_extra += (
                f"【用户记忆自评】{recall_score} / 5 分 → 按 SM-2 动态调整下一次复习间隔："
                "≥4 分则 ×1.5 延后（如第 7 天→第 10 天）；≤2 分则 ×0.5 提前（如第 4 天→第 2 天）"
                "并强制「重刺」（重新复习）；=3 分保持不变。"
                "请在计划表备注中说明「基于您的记忆自评，为您动态调整了复习间隔」。\n\n"
            )
        # ---------- 督学反馈：本周计划完成率（<50% 批评+番茄钟；>80% 表扬+加量） ----------
        feedback_prompt = build_tutor_feedback_prompt(
            completion_done, completion_total, completion_rate)
        if feedback_prompt:
            agent_extra += feedback_prompt + "\n\n"
        history_prompt = build_history_prompt(user_id)
        if history_prompt:
            agent_extra += history_prompt + "\n\n"
        # ---------- 长期记忆注入：最近 5 条（提问/打卡/AI 反馈）→ 个性化 Context ----------
        memory_prompt = build_memory_prompt(user_id, limit=5)
        if memory_prompt:
            agent_extra += memory_prompt + "\n\n"
        if is_focused_time:
            agent_extra += (
                "【精准定位 · 强约束】用户的消息包含明确的时间限定词。"
                "用户只需要特定时间段的任务安排。"
                "请绝对不要输出整周或过长的表格。"
                "请仅针对该时间段，给出 2-3 条具体的、可执行的任务，"
                "并直接以清晰的列表形式回复，不要任何寒暄废话。\n\n"
            )
        if is_progress_query:
            agent_extra += (
                "【学习监督员模式】用户正在询问学习进度/完成情况。"
                "请依据上面的打卡进度评估完成率，给予口头表扬或温和督促，"
                "并给出下一步简短建议（150 字以内）。此场景无需调用任何工具。"
            )

        try:

            final_reply = None
            fallback_reason = None
            for event in run_agent(message, profile=profile, schedule_text=schedule_text,
                                   extra_prompt=agent_extra, user_schedule=schedule_rows,
                                   progress_data=progress_data, user_id=user_id,
                                   completion_rate=completion_rate,
                                   completion_done=completion_done,
                                   completion_total=completion_total,
                                   recall_score=recall_score):
                phase = event.get("phase")

                if phase == "think":
                    yield _sse("agent_step", {
                        "step": event.get("step"),
                        "type": "think",
                        "message": "Agent 思考: " + (event.get("content") or ""),
                    })
                elif phase == "tool":
                    ok = event.get("result", {}).get("success", False)
                    tool_name = event.get("tool") or "?"
                    yield _sse("agent_step", {
                        "step": event.get("step"),
                        "type": "tool",
                        "tool": tool_name,
                        "ok": ok,
                        "message": "Agent 决策：调用 " + tool_name
                                   + ("（✓）" if ok else "（✗ 失败）"),
                    })
                elif phase == "fallback":
                    fallback_reason = event.get("reason", "")
                    yield _sse("agent_step", {
                        "type": "fallback",
                        "message": f"降级规则引擎: {fallback_reason[:120]}",
                    })
                elif phase == "final":
                    final_reply = event.get("content")

            if final_reply is not None:
                # ---------- 生成成功后：自动提取「当前复习科目 + 打卡进度」落盘 ----------
                try:
                    append_record(
                        intent="auto",
                        subjects=extract_subjects(final_reply, profile, schedule_rows),
                        progress_data=progress_data,
                        reply=final_reply,
                        weak=(profile.get("weak") or ""),
                        user_id=user_id,
                    )
                    # ★ Memory：记录本次「提问 + 打卡情况 + AI 反馈」
                    save_memory(user_id, {
                        "type": "chat",
                        "ask": message,
                        "progress": progress_data,
                        "reply": final_reply,
                        "subjects": extract_subjects(final_reply, profile, schedule_rows),
                    })
                except Exception as e:
                    print(f"[History] 记录失败: {e}")
                yield _sse("final", {
                    "reply": final_reply,
                    "intent": intent,
                    "agent": agent,
                    "tool": "done",
                })
                return

            # ---------- 降级：规则引擎兜底（未删除） ----------
            yield _sse("tool", {
                "status": "loading",
                "message": "规则引擎兜底中（复用原排课流程）...",
            })
            reply = make_plan(message, profile=profile)
            try:
                append_record(
                    intent="planner",
                    subjects=extract_subjects(reply, profile, schedule_rows),
                    progress_data=progress_data,
                    reply=reply,
                    weak=(profile.get("weak") or ""),
                    user_id=user_id,
                )
                # ★ Memory：兜底路径同样记录
                save_memory(user_id, {
                    "type": "plan",
                    "ask": message,
                    "progress": progress_data,
                    "reply": reply,
                    "subjects": extract_subjects(reply, profile, schedule_rows),
                })
            except Exception as e:
                print(f"[History] 记录失败: {e}")
            yield _sse("final", {
                "reply": reply,
                "intent": intent,
                "agent": agent + " · 规则兜底",
                "tool": "fallback",
            })
        except Exception as e:
            yield _sse("error", {"error": f"调用 DeepSeek 失败: {e}"})

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # 禁用代理缓冲，保证事件实时推送
        },
    )


# ================= Proactive Agent：自主巡航（主动推送） =================
# 触发方式（已移除“定时兜底”：不再有「30 秒首弹」与「每 2 分钟定时弹」）：
#   1. manual —— 快捷键 Ctrl+Shift+L / 点击顶部巡航栏
#   2. slot   —— 每天早晚固定时间点（08:00 早上 / 20:00 晚上）
#   3. event  —— 任务积累未完成（到期复习点、未完成任务、连续未打卡、考试 T-3）
# 防死循环：独立接口只做「一次」大模型调用，绝不回调 /api/chat 或自身；
#           仅接受 "[系统自主触发]" 指令；各触发自带去重。

PROACTIVE_MANUAL_MIN_GAP = 3         # 手动触发最短间隔，防按键连发
PROACTIVE_DAILY_LIMIT = 20           # 每日推送总量上限（兜底防刷）
EVENT_COOLDOWN_SECONDS = 30 * 60     # 同一事件的最短重复间隔

# ---- 任务积累未完成 / 事件阈值 ----
UNDONE_THRESHOLD = 3                 # 未完成任务积累 >= 阈值 -> backlog 事件
DUE_REVIEW_THRESHOLD = 3             # 到期复习点数量 >= 阈值 -> 事件
NO_CHECKIN_DAYS = 3                  # 连续 N 天未打卡 -> 事件
EXAM_T_MINUS = 3                     # 距考试 <= N 天 -> 事件

_proactive_state = {"day": "", "count": 0}
_last_manual_ts = 0.0                # 上一次「手动」触发时间
_event_last = {}                     # {event_type: 上次触发时间戳}
_slot_last = {}                      # {"YYYY-MM-DD:morning": 1} 每天每时段只推一次


def _parse_exam_date(s: str):
    """解析考试日期（支持 2026-11-15 / 2026/11/15 / 2026年11月15日）"""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(s, fmt).date()
        except Exception:
            continue
    return None


def eval_proactive_events(profile: dict, days_since_checkin: int, undone_count: int = -1) -> list:
    """评估「任务积累未完成」类事件，返回命中事件列表 [{"type":..., "detail":...}]

    1) 到期复习点超阈值（读 review_tasks.json，艾宾浩斯到期且未完成）
    2) 未完成任务积累超阈值（前端上报 undone_count）
    3) 连续 N 天未打卡（前端上报 days_since_checkin）
    4) 考试 T-3 天（学习画像里的考试日期）
    """
    events = []

    # 1) 到期复习点超阈值
    try:
        due = tool_query_due_reviews({"days": 0})   # days=0 -> 截至今天
        cnt = int(due.get("count", 0))
        if cnt >= DUE_REVIEW_THRESHOLD:
            events.append({"type": "due_reviews",
                           "detail": f"有 {cnt} 个到期复习点待完成"})
    except Exception:
        pass

    # 2) 未完成任务积累超阈值
    if isinstance(undone_count, int) and undone_count >= UNDONE_THRESHOLD:
        events.append({"type": "backlog",
                       "detail": f"今日已积累 {undone_count} 项任务未完成"})

    # 3) 连续 N 天未打卡
    if isinstance(days_since_checkin, int) and days_since_checkin >= NO_CHECKIN_DAYS:
        events.append({"type": "no_checkin",
                       "detail": f"已连续 {days_since_checkin} 天未打卡"})

    # 4) 考试 T-3 天
    today = datetime.now().date()
    for s in (profile or {}).get("subjects", []) or []:
        d = _parse_exam_date(s.get("examDate") or "")
        if not d:
            continue
        delta = (d - today).days
        if 0 <= delta <= EXAM_T_MINUS:
            events.append({"type": "exam_soon",
                           "detail": f"{s.get('subject') or '考试'}还有 {delta} 天开考"})

    return events

PROACTIVE_SYSTEM_PROMPT = (
    "你是「智学矩阵」的自主巡航 Agent。你不被动等待提问，而是主动关心用户的学习状态。\n"
    "请依据下方【情境】生成一句简短、温暖、可执行的主动提醒（60 字以内，只输出提醒正文）。\n"
    "要求：\n"
    "1. 结合当前真实时间与所处时段（上午/下午/晚上），语气自然、像朋友提醒。\n"
    "2. 若用户今天还有未打卡的任务，温和督促，并点名“哪一科 / 哪条任务”。\n"
    "3. 若临近空闲时段，给出“接下来可以做什么”的具体建议。\n"
    "4. 若当前时段与课表冲突（正在上课），提醒不要打扰、课后再说。\n"
    "5. 不要输出 Markdown 标题、不要分点、不要反问太多，一句话到位。"
)


def _time_slot(hour: int) -> str:
    """小时 → 时段（上午/下午/晚上/深夜）"""
    if 5 <= hour < 12:
        return "上午"
    if 12 <= hour < 18:
        return "下午"
    if 18 <= hour < 23:
        return "晚上"
    return "深夜"


def build_proactive_context(profile, schedule_text: str, progress_data: str) -> str:
    """组装自主决策情境：当前时间/时段 + 学习画像 + 课表 + 打卡进度 + 历史记忆"""
    now = datetime.now()
    slot = _time_slot(now.hour)
    parts = [f"【当前时间】{get_now_str()}（{slot}）"]

    profile_prompt = build_profile_prompt(profile)
    if profile_prompt:
        parts.append(profile_prompt)
    if schedule_text:
        parts.append("【用户课表】" + schedule_text)
    parts.append(
        "【今日打卡进度】" + (progress_data or "暂无打卡记录（今天可能还没开始学习）")
    )

    # 读取 user_history.json（历史复习科目 + 进度），体现"知道用户之前复习到哪"
    hist = load_history()
    if hist:
        last = hist[-1]
        bits = []
        if last.get("subjects"):
            bits.append("上次复习科目：" + "、".join(last["subjects"]))
        if last.get("progress"):
            bits.append("上次进度：" + str(last["progress"]))
        if bits:
            parts.append("【历史记忆】" + "；".join(bits))

    return "\n".join(parts)


@app.route("/api/proactive", methods=["POST"])
def proactive():
    """Proactive Agent 入口：仅三类触发 manual / slot / event（无定时兜底）

    触发优先级：manual（快捷键） > slot（早晚时间点） > event（任务积累未完成）
    """
    global _proactive_state, _last_manual_ts
    data = request.get_json(silent=True) or {}
    msg = (data.get("message") or "").strip()

    # 1. 只接受系统触发指令（普通消息不处理，避免被滥用）
    if not msg.startswith("[系统自主触发]"):
        return jsonify({"skip": True, "reason": "not_system_trigger"}), 200
    if not DEEPSEEK_API_KEY:
        return jsonify({"skip": True, "reason": "no_api_key"}), 200

    now_ts = time.time()
    today = datetime.now().strftime("%Y-%m-%d")
    if _proactive_state["day"] != today:
        _proactive_state = {"day": today, "count": 0}
        _event_last.clear()
        _slot_last.clear()

    # 每日总量上限（兜底防刷）
    if _proactive_state["count"] >= PROACTIVE_DAILY_LIMIT:
        return jsonify({"skip": True, "reason": "daily_limit"}), 200

    manual = bool(data.get("manual"))
    slot_key = (data.get("slot") or "").strip()        # morning | evening
    try:
        days_since = int(data.get("days_since_checkin"))
    except (TypeError, ValueError):
        days_since = -1
    try:
        undone = int(data.get("undone_count"))
    except (TypeError, ValueError):
        undone = -1

    profile = data.get("profile") or {}
    schedule_text = (data.get("schedule") or "").strip()
    progress_data = (data.get("progress_data") or "").strip()
    user_id = (data.get("user_id") or "").strip() or "default"

    # 「任务积累未完成」类事件评估
    events = eval_proactive_events(profile, days_since, undone)

    # ---- 决策：manual > slot > event（已无 interval 定时兜底）----
    reason_text = ""
    active_events = []
    slot_to_commit = ""

    if manual:
        if now_ts - _last_manual_ts < PROACTIVE_MANUAL_MIN_GAP:
            return jsonify({"skip": True, "reason": "manual_too_soon"}), 200
        trigger, active_events = "manual", events

    elif slot_key in ("morning", "evening"):
        skey = today + ":" + slot_key
        if _slot_last.get(skey):
            return jsonify({"skip": True, "reason": "slot_already_sent"}), 200
        trigger = "slot"
        slot_to_commit = skey
        active_events = events
        if slot_key == "morning":
            reason_text = ("现在是早上，请提醒用户开始今天的学习，"
                           "并结合课表空隙给出「上午可以立刻做的一件事」。")
        else:
            reason_text = ("现在是晚上，请提醒用户复盘今天的学习完成情况，"
                           "并给出「明天的一件优先事项」。")

    else:
        fresh = [e for e in events
                 if now_ts - _event_last.get(e["type"], 0) >= EVENT_COOLDOWN_SECONDS]
        if not fresh:
            # 无满足条件的事件 -> 不推送（不再有定时兜底）
            return jsonify({"skip": True, "reason": "no_trigger"}), 200
        trigger, active_events = "event", fresh

    # ---- 组装情境 + 触发原因 -> 单次大模型调用 ----
    context = build_proactive_context(profile, schedule_text, progress_data)
    # ★ 督学反馈：本周计划完成率（主动提醒同样受督学约束）
    f_done, f_total, f_rate = parse_completion(data)
    feedback_prompt = build_tutor_feedback_prompt(f_done, f_total, f_rate)
    if feedback_prompt:
        context += "\n\n" + feedback_prompt
    memory_prompt = build_memory_prompt(user_id, limit=5)      # ★ 长期记忆
    if memory_prompt:
        context += "\n\n" + memory_prompt
    if not reason_text and active_events:
        reason_text = "、".join(e["detail"] for e in active_events)
    if reason_text:
        context += "\n【触发原因】" + reason_text + "，请围绕该原因给出针对性的主动提醒。"

    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": PROACTIVE_SYSTEM_PROMPT + "\n\n" + context},
                {"role": "user", "content": "请根据当前情境，给我一句主动提醒。"},
            ],
            stream=False,
        )
        reply = (response.choices[0].message.content or "").strip()
    except Exception as e:
        return jsonify({"skip": True, "reason": f"llm_error: {e}"}), 200
    if not reply:
        return jsonify({"skip": True, "reason": "empty_reply"}), 200

    # ---- 记录状态（计数 + 各触发去重）----
    _proactive_state["count"] += 1
    if manual:
        _last_manual_ts = now_ts
    if slot_to_commit:
        _slot_last[slot_to_commit] = now_ts
    for e in active_events:
        _event_last[e["type"]] = now_ts

    return jsonify({
        "proactive": True,
        "reply": reply,
        "trigger": trigger,
        "events": active_events,
        "reason_text": reason_text,
        "slot": slot_key or _time_slot(datetime.now().hour),
        "time": get_now_str(),
        "count_today": _proactive_state["count"],
    })


# ================= Memory：长期记忆接口 =================

@app.route("/api/memory/checkin", methods=["POST"])
def memory_checkin():
    """打卡情况写入长期记忆（前端勾选任务时 fire-and-forget 上报）"""
    data = request.get_json(silent=True) or {}
    user_id = (data.get("user_id") or "").strip() or "default"
    progress = (data.get("progress") or "").strip()
    try:
        undone = int(data.get("undone"))
    except (TypeError, ValueError):
        undone = -1
    text = progress or "（无打卡进度文本）"
    if undone >= 0:
        text += f"；未完成 {undone} 项"
    rec = save_memory(user_id, {"type": "checkin", "progress": text})
    return jsonify({"ok": True, "record": rec})


@app.route("/api/memory", methods=["GET"])
def memory_list():
    """查看某用户最近记忆（默认 5 条，便于调试/演示）"""
    user_id = (request.args.get("user_id") or "").strip() or "default"
    try:
        limit = int(request.args.get("limit") or 5)
    except (TypeError, ValueError):
        limit = 5
    return jsonify({"user_id": user_id, "records": load_recent_memory(user_id, limit)})


# ================= 知识图谱（Knowledge Graph） =================

@app.route("/api/kg", methods=["GET"])
def kg_view():
    """查看知识图谱（答辩展示用）：?subject=高数&topic=级数"""
    subject = (request.args.get("subject") or "").strip()
    topic = (request.args.get("topic") or "").strip()
    try:
        depth = int(request.args.get("depth") or 2)
    except (TypeError, ValueError):
        depth = 2

    if not subject:
        return jsonify({"subjects": kg_list_subjects()})   # 未指定科目：列出全部科目

    ctx = build_kg_context(subject, topic, depth=depth)
    node = ctx.get("topic") or {}
    return jsonify({
        "subject": ctx.get("subject"),
        "topic": node.get("name", ""),
        "chapter": node.get("chapter", ""),
        "prerequisites": [p["name"] for p in ctx.get("prerequisites", [])],
        "successors": [s["name"] for s in ctx.get("successors", [])],
        "kg_text": ctx.get("text", ""),
    })


# ================= 遗忘刺客核心 API（P0 落库版） =================
# 复习 Agent（出刺）：知识点登记 / 今日必刺 / 记忆自评
# 排程 Agent（踩点）：计划条目查询 / 打卡 / 本周完成率（SQL 统计）
# Tutor Agent（出题）：作答判分回写 quiz_log

@app.route("/api/review/register", methods=["POST"])
def review_register():
    """登记知识点 → 生成 5 档艾宾浩斯复习计划

    请求体: {"user_id": str?, "subject": str, "content": str, "first_learn_date": "YYYY-MM-DD"?}
    """
    data = request.get_json(silent=True) or {}
    user_id = (data.get("user_id") or "").strip() or "default"
    subject = (data.get("subject") or "").strip()
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "content（知识点内容）不能为空"}), 400
    if not subject:
        subject = "综合"
    rec = review_engine.register_kp(
        user_id, subject, content,
        first_learn_date=(data.get("first_learn_date") or ""),
    )
    if "error" in rec:
        return jsonify({"error": rec["error"]}), 400
    return jsonify({"ok": True, **rec})


@app.route("/api/review/targets", methods=["GET"])
def review_targets():
    """今日必刺清单：?user_id=&days_ahead=0（截至今天/未来 N 天）"""
    user_id = (request.args.get("user_id") or "").strip() or "default"
    try:
        days_ahead = int(request.args.get("days_ahead") or 0)
    except (TypeError, ValueError):
        days_ahead = 0
    targets = review_engine.daily_targets(user_id, days_ahead)
    return jsonify({"user_id": user_id, "count": len(targets), "targets": targets})


@app.route("/api/review/score", methods=["POST"])
def review_score():
    """提交记忆自评（1~5）→ SM-2 真实算法动态调整下次出刺日期

    请求体: {"user_id": str?, "subject": str, "content": str, "memory_score": int}
    """
    data = request.get_json(silent=True) or {}
    user_id = (data.get("user_id") or "").strip() or "default"
    subject = (data.get("subject") or "").strip()
    content = (data.get("content") or "").strip()
    try:
        score = int(data.get("memory_score"))
    except (TypeError, ValueError):
        return jsonify({"error": "memory_score 必须是 1~5 整数"}), 400
    result = review_engine.submit_score(user_id, subject, content, score)
    if "error" in result:
        return jsonify({"error": result["error"]}), 404
    return jsonify({"ok": True, **result})


@app.route("/api/review/kps", methods=["GET"])
def review_kps():
    """列出某用户全部知识点（看板/调试）：?user_id=&limit=50"""
    user_id = (request.args.get("user_id") or "").strip() or "default"
    try:
        limit = int(request.args.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    return jsonify({"user_id": user_id, "kps": review_engine.list_kps(user_id, limit)})


@app.route("/api/plan/items", methods=["GET"])
def plan_items():
    """最近计划条目（结构化 plan_item 表）：?user_id=&limit=50"""
    user_id = (request.args.get("user_id") or "").strip() or "default"
    try:
        limit = int(request.args.get("limit") or 50)
    except (TypeError, ValueError):
        limit = 50
    return jsonify({"user_id": user_id,
                    "items": plan_store.list_recent_items(user_id, limit)})


@app.route("/api/plan/check", methods=["POST"])
def plan_check():
    """计划打卡：更新 plan_item 状态（督学数据来源）

    请求体: {"plan_item_id": int, "status": 0|1|2, "user_id": str?}
    """
    data = request.get_json(silent=True) or {}
    try:
        item_id = int(data.get("plan_item_id"))
        status = int(data.get("status"))
    except (TypeError, ValueError):
        return jsonify({"error": "plan_item_id / status 必须是整数"}), 400
    result = plan_store.mark_plan_item(item_id, status, (data.get("user_id") or "").strip())
    if "error" in result:
        return jsonify({"error": result["error"]}), 400
    return jsonify({"ok": True, **result})


@app.route("/api/plan/completion", methods=["GET"])
def plan_completion():
    """本周计划完成率（SQL 统计）：?user_id= → {done, total, rate, text}"""
    user_id = (request.args.get("user_id") or "").strip() or "default"
    return jsonify(plan_store.week_completion(user_id))


@app.route("/api/quiz/answer", methods=["POST"])
def quiz_answer():
    """出题-判题闭环：提交作答 → 大模型判分 → 回写 quiz_log

    请求体: {"quiz_id": int?, "user_id": str?, "subject": str?, "answer": str}
    quiz_id 缺省时取该用户最近一道未作答题目。
    """
    data = request.get_json(silent=True) or {}
    user_id = (data.get("user_id") or "").strip() or "default"
    answer = (data.get("answer") or "").strip()
    if not answer:
        return jsonify({"error": "answer（作答内容）不能为空"}), 400
    quiz_id = data.get("quiz_id")
    row = None
    if quiz_id is not None:
        try:
            row = query_one(
                "SELECT * FROM quiz_log WHERE id = ? AND user_id = ?",
                (int(quiz_id), user_id),
            )
        except (TypeError, ValueError):
            row = None
    if row is None:
        row = query_one(
            "SELECT * FROM quiz_log WHERE user_id = ? AND answer IS NULL "
            "ORDER BY id DESC LIMIT 1", (user_id,),
        )
    if row is None:
        return jsonify({"error": "没有找到待作答的题目，请先出题"}), 404

    if not DEEPSEEK_API_KEY:
        return jsonify({"error": "服务端未配置 DEEPSEEK_API_KEY"}), 400

    try:
        response = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system",
                 "content": "你是「遗忘刺客」判题专家。根据题目与用户作答判定对错。"
                            "题目在作答后通常附有「考点/答题要点」行，请以其为判定标准。"
                            "严格输出 JSON：{\"correct\": true|false, \"comment\": \"点评（80字内，"
                            "指出对错、考点、改进建议）\"}，不要输出其他内容。"},
                {"role": "user",
                 "content": f"【题目】\n{row['question']}\n\n【用户作答】\n{answer}"},
            ],
            stream=False,
        )
        raw = (response.choices[0].message.content or "").strip()
        m = re.search(r"\{.*\}", raw, re.S)
        verdict = json.loads(m.group(0)) if m else {}
        correct = bool(verdict.get("correct", False))
        comment = (verdict.get("comment") or "").strip()
    except Exception as e:
        return jsonify({"error": f"判题失败: {e}"}), 500

    from agents.storage import execute as _execute
    _execute("UPDATE quiz_log SET answer = ?, correct = ? WHERE id = ?",
             (answer[:2000], 1 if correct else 0, row["id"]))
    return jsonify({
        "ok": True, "quiz_id": row["id"], "subject": row["subject"],
        "correct": correct, "comment": comment,
    })


@app.route("/api/quiz/history", methods=["GET"])
def quiz_history():
    """出题-判题历史：?user_id=&limit=20"""
    user_id = (request.args.get("user_id") or "").strip() or "default"
    try:
        limit = int(request.args.get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    from agents.storage import query_all as _query_all
    rows = _query_all(
        "SELECT id, subject, topic, question, answer, correct, created_at "
        "FROM quiz_log WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    )
    return jsonify({"user_id": user_id,
                    "records": [dict(r) for r in rows]})


if __name__ == "__main__":
    # host=0.0.0.0 便于手机浏览器通过局域网访问
    app.run(host="0.0.0.0", port=5000, debug=True)
