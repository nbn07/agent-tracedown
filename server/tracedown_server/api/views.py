"""Read models for the UI: span tree and agent network graph."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional


def build_tree(spans: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Nested span tree, children ordered by step."""
    kids: Dict[Optional[str], List[Dict[str, Any]]] = defaultdict(list)
    for s in spans:
        kids[s["parent_span_id"]].append(s)

    def node(s):
        ch = sorted(kids.get(s["span_id"], []), key=lambda x: x["attributes"].get("tdn.step", 0))
        return {"span_id": s["span_id"], "name": s["name"], "kind": s["kind"],
                "step": s["attributes"].get("tdn.step"), "status": s["status"]["code"],
                "children": [node(c) for c in ch]}

    return [node(r) for r in kids.get(None, [])]


def build_graph(spans: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Agent network for React Flow: agents, tools, retrievers and the calls between them."""
    by_id = {s["span_id"]: s for s in spans}
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: Dict[tuple, Dict[str, Any]] = {}

    def owner(s) -> Optional[Dict[str, Any]]:
        p = by_id.get(s["parent_span_id"]) if s["parent_span_id"] else None
        while p is not None and p["kind"] != "agent":
            p = by_id.get(p["parent_span_id"]) if p["parent_span_id"] else None
        return p

    def node_for(s) -> str:
        a = s["attributes"]
        if s["kind"] == "agent":
            nid, label = "agent:" + a.get("gen_ai.agent.name", s["name"]), a.get("gen_ai.agent.name", s["name"])
        elif s["kind"] == "tool":
            nid, label = "tool:" + a.get("gen_ai.tool.name", s["name"]), a.get("gen_ai.tool.name", s["name"])
        elif s["kind"] == "llm":
            nid, label = "llm:" + str(a.get("gen_ai.request.model")), str(a.get("gen_ai.request.model"))
        elif s["kind"] == "retrieval":
            nid, label = "retrieval:" + str(a.get("tdn.retrieval.source") or "retriever"), \
                str(a.get("tdn.retrieval.source") or "retriever")
        else:
            nid, label = f"{s['kind']}:{s['name']}", s["name"]
        n = nodes.setdefault(nid, {"id": nid, "type": s["kind"], "label": label, "calls": 0, "errors": 0})
        n["calls"] += 1
        n["errors"] += s["status"]["code"] == "ERROR"
        return nid

    for s in sorted(spans, key=lambda x: x["attributes"].get("tdn.step", 0)):
        nid = node_for(s)
        o = owner(s)
        if o is None:
            continue
        src = "agent:" + o["attributes"].get("gen_ai.agent.name", o["name"])
        if s["kind"] == "handoff":
            src = "agent:" + str(s["attributes"].get("tdn.handoff.from"))
            nid = "agent:" + str(s["attributes"].get("tdn.handoff.to"))
            nodes.pop(f"handoff:{s['name']}", None)
        e = edges.setdefault((src, nid), {"source": src, "target": nid, "calls": 0, "errors": 0, "steps": []})
        e["calls"] += 1
        e["errors"] += s["status"]["code"] == "ERROR"
        e["steps"].append(s["attributes"].get("tdn.step"))
    return {"nodes": list(nodes.values()), "edges": list(edges.values())}


def merge_graphs(graphs: List[Dict[str, Any]], runs: int = 0) -> Dict[str, Any]:
    """Union of per-run graphs: totals per node and edge, plus how many runs used each."""
    nodes: Dict[str, Dict[str, Any]] = {}
    edges: Dict[tuple, Dict[str, Any]] = {}
    for g in graphs:
        for n in g["nodes"]:
            e = nodes.setdefault(n["id"], {"id": n["id"], "type": n["type"], "label": n["label"],
                                           "calls": 0, "errors": 0, "runs": 0})
            e["calls"] += n["calls"]
            e["errors"] += n["errors"]
            e["runs"] += 1
        for ed in g["edges"]:
            e = edges.setdefault((ed["source"], ed["target"]), {"source": ed["source"], "target": ed["target"],
                                                                 "calls": 0, "errors": 0, "runs": 0})
            e["calls"] += ed["calls"]
            e["errors"] += ed["errors"]
            e["runs"] += 1
    for n in nodes.values():
        n["error_rate"] = round(n["errors"] / n["calls"], 4) if n["calls"] else 0
    for e in edges.values():
        e["error_rate"] = round(e["errors"] / e["calls"], 4) if e["calls"] else 0
    return {"runs": runs, "nodes": list(nodes.values()), "edges": list(edges.values())}
