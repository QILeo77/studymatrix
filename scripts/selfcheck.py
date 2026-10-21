"""StudyMatrix 本地自查脚本
用法:
    1. 终端 A: python app.py          # 启动服务
    2. 终端 B: python scripts/selfcheck.py
退出码: 0 全部通过, 1 存在失败项
"""

import json
import re
import urllib.request
from pathlib import Path

BASE_URL = "http://127.0.0.1:5000"
TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")  # 合法 HH:MM
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五"]


def to_minutes(t: str) -> int:
    """'09:40' -> 580，便于时长比较"""
    h, m = t.split(":")
    return int(h) * 60 + int(m)


# ---------- ① /api/health 接口检查 ----------
def check_health():
    results = []
    try:
        # 基础健康检查
        with urllib.request.urlopen(f"{BASE_URL}/api/health", timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            results += [
                ("HTTP 状态码为 200", resp.status == 200),
                ("返回 project=StudyMatrix", body.get("project") == "StudyMatrix"),
                ("返回 status=ok", body.get("status") == "ok"),
            ]
        # CORS 跨域头检查（伪造第三方来源）
        req = urllib.request.Request(
            f"{BASE_URL}/api/health", headers={"Origin": "http://example.com"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            cors = resp.headers.get("Access-Control-Allow-Origin")
            results.append(("CORS 跨域头生效", cors is not None))
    except Exception as e:  # 服务未启动等
        results.append((f"服务可访问（错误: {e}）", False))
    return results


# ---------- ② 响应式前端检查 ----------
def check_responsive():
    results = []
    try:
        html = urllib.request.urlopen(f"{BASE_URL}/", timeout=5).read().decode("utf-8")
        css = urllib.request.urlopen(f"{BASE_URL}/static/style.css", timeout=5).read().decode("utf-8")
    except Exception as e:
        return [(f"首页/CSS 可访问（错误: {e}）", False)]

    results += [
        # HTML 层：移动端自适应的关键标志
        ("含 viewport 自适应标签", 'name="viewport"' in html and "width=device-width" in html),
        ("含移动端主题色配置", 'name="theme-color"' in html),
        ("含聊天输入框 chatInput", 'id="chatInput"' in html),
        ("含发送按钮 sendBtn", 'id="sendBtn"' in html),
        # 新版 UI 关键元素
        ("含智学矩阵品牌标题", "智学矩阵" in html),
        ("含新建对话按钮", "新建对话" in html),
        ("含三大功能导航", all(k in html for k in ["智能复习规划", "空闲课表查询", "模拟出题测试"])),
        ("含多 Agent 调度中心状态栏", "多 Agent 调度中心 · 待命中" in html),
        # 技术栈检查
        ("引入 Tailwind CDN", "cdn.tailwindcss.com" in html),
        ("引入 marked.js CDN", "marked.min.js" in html),
        # 交互逻辑检查
        ("JS 已接入后端 /api/chat", "/api/chat" in html),
        ("JS 使用 fetch POST 调用", "fetch(API_URL" in html),
        ("JS 渲染 AI Markdown 回复", 'renderMarkdown(payload.reply' in html),
        ("假回复逻辑已删除", "FIXED_REPLY" not in html),
        # CSS 层：手机适配的关键特性
        ("CSS 含媒体查询 @media", "@media" in css),
        ("CSS 呼吸灯动画 breathe", "breathe" in css),
        ("CSS 打字机动画 bounceDot", "bounceDot" in css),
        ("CSS Markdown 表格样式", "md-body table" in css),
        ("CSS 手机端抽屉 drawer", "drawer" in css),
        ("CSS 翡翠绿主色 #10b981", "#10b981" in css),
    ]
    return results


# ---------- ②' /api/chat 对话接口检查（SSE 流式） ----------
def _parse_sse(raw: str):
    """解析 SSE 响应文本 -> [(event, payload_dict), ...]"""
    events = []
    for chunk in raw.split("\n\n"):
        if not chunk.strip():
            continue
        event, data_str = "", ""
        for line in chunk.split("\n"):
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data_str += line[5:].strip()
        try:
            payload = json.loads(data_str) if data_str else {}
        except json.JSONDecodeError:
            payload = {}
        events.append((event, payload))
    return events


def check_chat():
    results = []
    # 参数校验：空消息应在流中返回 error 事件
    try:
        req = urllib.request.Request(
            f"{BASE_URL}/api/chat",
            data=json.dumps({"message": ""}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            events = _parse_sse(resp.read().decode("utf-8"))
            has_error = any(e == "error" for e, _ in events)
            results.append(("空消息返回 error 事件（参数校验）", has_error))
    except Exception as e:
        results.append((f"空消息校验可执行（错误: {e}）", False))

    # 真实调用：闲聊应返回 router + final 事件
    try:
        req = urllib.request.Request(
            f"{BASE_URL}/api/chat",
            data=json.dumps({"message": "用一句话自我介绍"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=90) as resp:
            ctype = resp.headers.get("Content-Type", "")
            raw = resp.read().decode("utf-8")
            events = _parse_sse(raw)
            names = [e for e, _ in events]
            final = next((p for e, p in events if e == "final"), {})
            reply = final.get("reply", "")
            results += [
                ("chat: 响应为 text/event-stream", "text/event-stream" in ctype),
                ("chat: 返回 router 事件", "router" in names),
                ("chat: 返回 final 事件且 reply 非空", isinstance(reply, str) and len(reply) > 0),
                ("chat: 回复含中文（DeepSeek 正常应答）", any("\u4e00" <= ch <= "\u9fff" for ch in reply)),
            ]
    except Exception as e:
        results.append((f"chat 真实调用（错误: {e}）", False))

    # 真实调用：排计划应返回 router + tool + final 事件序列
    try:
        req = urllib.request.Request(
            f"{BASE_URL}/api/chat",
            data=json.dumps({"message": "帮我制定一份下周的复习计划"}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            events = _parse_sse(raw)
            names = [e for e, _ in events]
            final = next((p for e, p in events if e == "final"), {})
            tool = next((p for e, p in events if e == "tool"), {})
            reply = final.get("reply", "")
            results += [
                ("planner: 返回 router 事件", "router" in names),
                ("planner: 返回 tool 事件（正在读取教务系统）", "tool" in names and "schedule.json" in tool.get("message", "")),
                ("planner: 返回 final 事件", "final" in names),
                ("planner: 回复含 Markdown 表格（时间/任务/目标）", "| 时间" in reply or "|时间" in reply or "时间 |" in reply),
                ("planner: 回复引用真实空闲时段（来自课表工具）", "11:40" in reply or "15:40" in reply),
            ]
    except Exception as e:
        results.append((f"planner 真实调用（错误: {e}）", False))
    return results


# ---------- ③ 课程表规则检查 ----------
def check_schedule():
    path = Path(__file__).resolve().parent.parent / "data" / "schedule.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return [(f"schedule.json 可解析（错误: {e}）", False)]

    week = data.get("week_schedule", {})
    results = [
        ("包含周一至周五全部 5 天", all(d in week for d in WEEKDAYS)),
        ("含 daily_bounds 每日边界", "daily_bounds" in data),
    ]
    if "daily_bounds" not in data:
        return results

    b_start = to_minutes(data["daily_bounds"]["start"])
    b_end = to_minutes(data["daily_bounds"]["end"])

    for day, courses in week.items():
        # 1. 字段完整 + 时间格式合法
        results.append((
            f"{day}: 课程字段完整且时间格式合法",
            all(
                {"course", "start", "end"} <= set(c)
                and TIME_RE.match(c["start"]) and TIME_RE.match(c["end"])
                for c in courses
            ),
        ))
        # 2. 起止时间无倒置
        results.append((
            f"{day}: 上课时间 start < end",
            all(to_minutes(c["start"]) < to_minutes(c["end"]) for c in courses),
        ))
        # 3. 课程在每日边界内（08:00–22:00）
        results.append((
            f"{day}: 课程在 daily_bounds 范围内",
            all(b_start <= to_minutes(c["start"]) and to_minutes(c["end"]) <= b_end for c in courses),
        ))
        # 4. 同日课程不重叠
        cs = sorted(courses, key=lambda c: to_minutes(c["start"]))
        results.append((
            f"{day}: 同日课程无重叠",
            all(to_minutes(a["end"]) <= to_minutes(b["start"]) for a, b in zip(cs, cs[1:])),
        ))
        # 5. 至少有一段 ≥60 分钟空闲（排复习计划的前提）
        gaps, prev = [], b_start
        for c in cs:
            if to_minutes(c["start"]) - prev >= 60:
                gaps.append((prev, to_minutes(c["start"])))
            prev = to_minutes(c["end"])
        if b_end - prev >= 60:
            gaps.append((prev, b_end))
        results.append((f"{day}: 存在≥60分钟空闲段", len(gaps) > 0))

    return results


# ---------- 汇总输出 ----------
def report(title, results):
    print(f"\n=== {title} ===")
    for name, ok in results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return sum(ok for _, ok in results), len(results)


if __name__ == "__main__":
    total_pass = total = 0
    suites = [
        ("① /api/health 接口", check_health),
        ("② 响应式前端（新版 UI）", check_responsive),
        ("②' /api/chat 对话接口", check_chat),
        ("③ 课程表规则", check_schedule),
    ]
    for title, fn in suites:
        p, t = report(title, fn())
        total_pass, total = total_pass + p, total + t

    print(f"\n总计: {total_pass}/{total} 项通过")
    raise SystemExit(0 if total_pass == total else 1)
