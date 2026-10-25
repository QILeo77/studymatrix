# -*- coding: utf-8 -*-
"""知识图谱（Knowledge Graph）测试：
  1) 单元：图谱查询 / 前置知识点回溯 / 工具注册
  2) 端到端：Tutor Agent 出题时基于图谱出「关联前置知识点」的综合应用题
"""
import json
import sys
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

out = []

# ---------- 1) 单元 ----------
from agents.kg import build_kg_context, list_subjects
from agents.tools_registry import TOOL_REGISTRY, get_tool_schemas, tool_query_knowledge_graph

out.append("[单元] 图谱科目: " + str(list_subjects()))
names = [t["function"]["name"] for t in get_tool_schemas()]
out.append("[单元] 工具清单含 query_knowledge_graph: " + str("query_knowledge_graph" in names))
assert "query_knowledge_graph" in names

kg = build_kg_context("高数", "级数", depth=2)
topic = (kg.get("topic") or {}).get("name", "")
prereqs = [p["name"] for p in kg.get("prerequisites", [])]
out.append("[单元] 高数→级数  前置: " + str(prereqs))
assert topic == "级数", topic
assert "函数与极限" in prereqs and "定积分" in prereqs, prereqs
out.append("[单元] 图谱上下文:\n" + kg["text"])

tool_res = tool_query_knowledge_graph({"subject": "数据结构", "topic": "二叉树遍历"})
out.append("[单元] 数据结构→二叉树遍历 前置: " + str(tool_res["prerequisites"]))
assert "树与二叉树" in tool_res["prerequisites"], tool_res

# ---------- 2) 端到端 ----------
URL = "http://127.0.0.1:5000/api/chat"


def post_chat(payload):
    req = urllib.request.Request(URL, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    events, final = [], ""
    with urllib.request.urlopen(req, timeout=180) as resp:
        buf = ""
        for raw in resp:
            buf += raw.decode("utf-8")
            while "\n\n" in buf:
                chunk, buf = buf.split("\n\n", 1)
                en, ds = "", ""
                for line in chunk.split("\n"):
                    if line.startswith("event:"):
                        en = line[6:].strip()
                    elif line.startswith("data:"):
                        ds += line[5:].strip()
                if not en:
                    continue
                data = json.loads(ds) if ds else {}
                events.append((en, data))
                if en == "final":
                    final = data.get("reply", "")
    return events, final


payload = {
    "user_id": "u_kgtest",
    "message": "针对高数级数，出一道综合应用题考考我",
    "profile": {"goal": "期末冲刺", "weak": "高数",
                "subjects": [{"subject": "高数", "examDate": ""}]},
}
events, reply = post_chat(payload)
tools = [d.get("tool") for en, d in events if en == "agent_step" and d.get("type") == "tool"]
out.append("\n[端到端] 工具决策: " + str(tools))
out.append("[端到端] 出题回复:\n" + reply[:600])

has_prereq_word = ("前置" in reply) or ("关联" in reply)
has_prereq_node = any(k in reply for k in ["极限", "定积分", "函数与极限", "导数", "求和"])
out.append("[端到端] 提及前置知识点字样: " + str(has_prereq_word) + " / 前置节点: " + str(has_prereq_node))
assert any(t == "generate_test" for t in tools), tools
assert has_prereq_word or has_prereq_node, "出题未体现图谱前置关联"

out.append("\n===== 知识图谱测试全部通过（跨节点出题能力已具备）=====")

Path(BASE / "_kg_out.txt").write_text("\n".join(out), encoding="utf-8")
print("done. prereq_in_reply=", has_prereq_word or has_prereq_node)