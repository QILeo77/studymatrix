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

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_from_directory
from flask_cors import CORS
from openai import OpenAI

from agents.planner import make_plan
from agents.router import identify_intent

# 加载 .env 环境变量
load_dotenv()

# DeepSeek API Key（从 .env 读取）
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

# 闲聊 Agent 系统提示词：智能学习助手人设
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

    def generate():
        if not message:
            yield _sse("error", {"error": "消息不能为空"})
            return

        if not DEEPSEEK_API_KEY:
            yield _sse("error", {"error": "服务端未配置 DEEPSEEK_API_KEY"})
            return

        # ---------- 1. Router Agent：识别意图 ----------
        intent = identify_intent(message)
        if intent == "planner":
            agent = "Planner Agent（学业规划师）"
        else:
            agent = "Chat Agent（学习助手）"
        yield _sse("router", {"intent": intent, "agent": agent})

        # ---------- 2. Planner：先调用课表工具（读取失败会打印控制台错误） ----------
        if intent == "planner":
            # 触发工具状态推送；get_free_time 失败时在控制台打印错误
            yield _sse("tool", {
                "status": "loading",
                "message": "正在读取本地教务系统 schedule.json...",
            })

            # make_plan 内部调用 get_free_time()（读取 data/schedule.json），
            # 并把空闲数据拼进系统提示词："已知该用户的真实空闲时间为：{...}，请根据这些时间回答用户。"
            try:
                reply = make_plan(message)
                tool_status = "done"
            except Exception as e:
                yield _sse("error", {"error": f"调用 DeepSeek 失败: {e}"})
                return

            yield _sse("final", {
                "reply": reply,
                "intent": intent,
                "agent": agent,
                "tool": tool_status,
            })
            return

        # ---------- 3. Chat：正常调用大模型回复 ----------
        try:
            response = client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": CHAT_PROMPT},
                    {"role": "user", "content": message},
                ],
                stream=False,
            )
            reply = response.choices[0].message.content or ""
            yield _sse("final", {"reply": reply, "intent": intent, "agent": agent})
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


if __name__ == "__main__":
    # host=0.0.0.0 便于手机浏览器通过局域网访问
    app.run(host="0.0.0.0", port=5000, debug=True)
