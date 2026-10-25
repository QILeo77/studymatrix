# agents/kg.py
# 知识图谱（Knowledge Graph）查询层
#   数据：data/kg.json（节点=知识点，边=prerequisite 前置关联）
#   能力：定位知识点 → 回溯前置知识点链 → 生成可注入 Prompt 的图谱上下文
#   用途：Tutor Agent「基于知识图谱，出关联前置知识点的综合应用题」（跨节点出题）

import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
KG_PATH = BASE_DIR / "data" / "kg.json"

_cache = None


def load_kg(force: bool = False) -> dict:
    """读取知识图谱（带内存缓存）"""
    global _cache
    if _cache is not None and not force:
        return _cache
    try:
        data = json.loads(KG_PATH.read_text(encoding="utf-8"))
        _cache = data if isinstance(data, dict) else {"subjects": {}}
    except Exception as e:
        print(f"[KG] kg.json 读取失败: {e}")
        _cache = {"subjects": {}}
    return _cache


def list_subjects() -> list:
    """列出图谱中的科目名"""
    return list(load_kg().get("subjects", {}).keys())


def _norm_subject(subject: str):
    """按科目名或其别名匹配图谱科目（返回 (name, subject_block) 或 (None, None)）"""
    s = (subject or "").strip()
    if not s:
        return None, None
    subjects = load_kg().get("subjects", {})
    if s in subjects:
        return s, subjects[s]
    for name, block in subjects.items():
        aliases = block.get("aliases") or []
        if any(s == a or s in a or a in s for a in aliases):
            return name, block
    # 模糊：科目名互相包含（如「高等数学」~「高数」）
    for name in subjects:
        if s in name or name in s:
            return name, subjects[name]
    return None, None


def _nodes_map(subject_block: dict) -> dict:
    return {n["id"]: n for n in (subject_block.get("nodes") or [])}


def match_node(subject: str, topic_text: str):
    """在科目内定位知识点节点（按节点名子串匹配，取最长命中）"""
    _, block = _norm_subject(subject)
    if not block or not topic_text:
        return None
    text = topic_text.strip()
    best, best_len = None, 0
    for n in (block.get("nodes") or []):
        name = n.get("name") or ""
        if name and name in text and len(name) > best_len:
            best, best_len = n, len(name)
    return best


def _ancestors(subject_block: dict, node_id: str) -> dict:
    """回溯前置知识点：{node_id: 距离}（距离越小越靠近目标）"""
    seen = {}
    frontier = [(node_id, 0)]
    edges = subject_block.get("edges") or []
    while frontier:
        nid, dist = frontier.pop(0)
        for e in edges:
            if e.get("to") == nid and (e.get("type") or "prerequisite") == "prerequisite":
                src = e.get("from")
                if src and (src not in seen or seen[src] > dist + 1):
                    seen[src] = dist + 1
                    frontier.append((src, dist + 1))
    return seen


def _successors(subject_block: dict, node_id: str) -> list:
    """直接后继知识点"""
    return [e.get("to") for e in (subject_block.get("edges") or [])
            if e.get("from") == node_id and e.get("to")]


def build_kg_context(subject: str, topic_text: str = "", depth: int = 2) -> dict:
    """构建知识图谱上下文（供 Prompt 注入 / 工具返回）

    返回: {subject, topic, prerequisites:[node...], successors:[node...], text: str}
    """
    name, block = _norm_subject(subject)
    result = {"subject": name or (subject or "").strip(), "topic": None,
              "prerequisites": [], "successors": [], "text": ""}
    if not block:
        result["text"] = f"【知识图谱上下文】未收录科目「{subject or '—'}」，请按通用学科逻辑出题。"
        return result

    nodes = _nodes_map(block)
    node = match_node(name, topic_text)
    result["topic"] = node

    if node:
        anc = _ancestors(block, node["id"])
        order = {n["id"]: i for i, n in enumerate(block.get("nodes") or [])}
        # 按科目内节点声明顺序（由基础到进阶）排列前置知识点
        prereq_nodes = [nodes[i] for i in sorted(anc, key=lambda k: order.get(k, 999)) if i in nodes]
        prereq_nodes = prereq_nodes[:max(1, int(depth)) * 3]
        succ_nodes = [nodes.get(i) for i in _successors(block, node["id"]) if nodes.get(i)]
        result["prerequisites"] = prereq_nodes
        result["successors"] = succ_nodes

        chain = " → ".join(f"{n['name']}（{n.get('chapter','')}）" for n in prereq_nodes) or "—"
        succ = "、".join(f"{n['name']}（{n.get('chapter','')}）" for n in succ_nodes) or "—"
        lines = [
            "【知识图谱上下文（Knowledge Graph）】",
            f"- 科目：{name}",
            f"- 当前知识点：{node['name']}（{node.get('chapter','')}）",
            f"- 前置知识点（按依赖顺序，由基础到进阶）：{chain}",
            f"- 后继知识点：{succ}",
        ]
        if prereq_nodes:
            rels = "；".join(f"{p['name']} → {node['name']}" for p in prereq_nodes)
            lines.append(f"- 图谱关联边：{rels}")
        result["text"] = "\n".join(lines)
        return result

    # 未精确匹配到知识点：给出该科目的节点清单，供模型自选
    names = "、".join(f"{n['name']}（{n.get('chapter','')}）" for n in (block.get("nodes") or []))
    result["text"] = (
        "【知识图谱上下文（Knowledge Graph）】\n"
        f"- 科目：{name}\n"
        f"- 未精确匹配到知识点（用户表述：{topic_text or '—'}）\n"
        f"- 该科目图谱节点：{names}\n"
        "请据此选择最贴近的知识点，并回溯其前置知识点。"
    )
    return result