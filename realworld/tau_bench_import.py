"""Turn τ-bench's recorded failures into Tracedown traces with reference labels.

    pip install -e path/to/tau-bench          # for the tool definitions
    python realworld/tau_bench_import.py --tau-bench path/to/tau-bench

Writes ``realworld/data/{dev,test}.jsonl``. One line per failed run:
``{id, model, domain, task_id, trial, label, bundle}``. The label follows
``LABELING.md``; ``bundle`` is the same shape as ``GET /api/v1/runs/{id}/bundle``.

How a conversation becomes a trace:
* each assistant message is an LLM step; its input is whatever arrived since the agent
  last spoke (customer messages, tool results);
* each tool call is a tool step with its arguments, result and schema check; a result
  that starts with "Error" marks the step failed;
* the customer's first message is the run input, the agent's last message the output;
* the run is marked failed with τ-bench's reward, the way a production feedback
  signal would arrive.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "sdk", "python"))

import agent_tracedown as tdn  # noqa: E402
from agent_tracedown.tracer import ManualTrace, validate_args  # noqa: E402

READ_PREFIXES = ("get_", "list_", "search_", "find_")
READ_ONLY = {"calculate", "think"}
MODELS = {"gpt-4o": "gpt-4o", "sonnet-35-new": "claude-3-5-sonnet-20241022"}
SPLITS = {"dev": "gpt-4o", "test": "sonnet-35-new"}


def is_write(name: str) -> bool:
    return not (name.startswith(READ_PREFIXES) or name in READ_ONLY)


def canonical(x: Any) -> str:
    return json.dumps(x, sort_keys=True, separators=(",", ":"))


def tool_specs(domain: str) -> List[Dict[str, Any]]:
    if domain == "airline":
        from tau_bench.envs.airline.tools import ALL_TOOLS
    else:
        from tau_bench.envs.retail.tools import ALL_TOOLS
    out = []
    for t in ALL_TOOLS:
        f = t.get_info()["function"]
        out.append({"name": f["name"], "description": f.get("description", ""), "schema": f.get("parameters")})
    return out


def _args(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def _content(raw: Any) -> Any:
    if isinstance(raw, str) and raw[:1] in "{[":
        try:
            return json.loads(raw)
        except ValueError:
            pass
    return raw


# ----------------------------------------------------------------------------- label (LABELING.md)

def reference_label(calls: List[Dict[str, Any]], expected: List[Dict[str, Any]],
                    reward_info: Dict[str, Any], write=None) -> Dict[str, Any]:
    """``calls``: the agent's tool calls in order, each {name, args, call_id}.
    ``write``: which tool names change state (default: the τ-bench rule, ``is_write``)."""
    is_write = write or globals()["is_write"]
    # Only expected *write* actions matter: τ-bench's reward compares the database, and the
    # expected list also contains lookups (see LABELING.md, change 1).
    remaining = [dict(a) for a in expected if is_write(a["name"])]
    for c in calls:
        if not is_write(c["name"]):
            continue
        hit = next((i for i, a in enumerate(remaining)
                    if a["name"] == c["name"] and canonical(a.get("kwargs", {})) == canonical(c["args"])), None)
        if hit is not None:
            remaining.pop(hit)
            continue
        cat = "used_wrong_tool_argument" if any(a["name"] == c["name"] for a in remaining) else "called_wrong_tool"
        return {"category": cat, "call_id": c["call_id"], "tool": c["name"],
                "detail": f"{c['name']}({canonical(c["args"])})",
                "expected": [f"{a['name']}({canonical(a.get("kwargs", {}))})" for a in remaining]}
    if remaining:
        return {"category": "goal_partially_completed", "call_id": None,
                "detail": "missing: " + "; ".join(a["name"] for a in remaining),
                "expected": [f"{a['name']}({canonical(a.get("kwargs", {}))})" for a in remaining]}
    info = (reward_info or {}).get("info") or {}
    return {"category": "missing_output", "call_id": "__final__",
            "detail": f"all actions matched; r_outputs={info.get('r_outputs')}", "expected": []}


# ----------------------------------------------------------------------------- trace

def build_trace(record: Dict[str, Any], domain: str, model: str, specs: List[Dict[str, Any]]) -> Tuple[str, Dict]:
    mem = tdn.MemoryExporter()
    tdn.init(project="tau-bench", exporter=mem)
    traj = record["traj"]
    first_user = next((m.get("content") for m in traj if m.get("role") == "user"), None)
    t = ManualTrace(f"{domain.capitalize()}Agent", input=first_user,
                    **{"tdn.tau.task_id": record["task_id"], "tdn.tau.trial": record.get("trial")})
    t.declare_tools(specs)
    schema = {s["name"]: s["schema"] for s in specs}
    t.start("agent", f"invoke_agent {domain.capitalize()}Agent", "agent")
    pending: List[Dict[str, Any]] = []
    calls: List[Dict[str, Any]] = []
    open_calls: Dict[str, List[str]] = {}
    final_text: Optional[str] = None
    llm_n = 0
    for i, m in enumerate(traj):
        role = m.get("role")
        if role == "system":
            continue
        if role == "user":
            if i > 1 or pending or llm_n:  # the first customer message is the run input
                pending.append({"role": "user", "content": m.get("content")})
            continue
        if role == "assistant":
            llm_n += 1
            key = f"llm{llm_n}"
            tcs = m.get("tool_calls") or []
            out = {"text": m.get("content")}
            if tcs:
                out["tool_calls"] = [{"name": tc["function"]["name"], "arguments": _args(tc["function"]["arguments"])}
                                     for tc in tcs]
            t.start(key, f"chat {model}", "llm", parent="agent", input=pending or None,
                    attrs={"gen_ai.request.model": model, "gen_ai.provider.name": "openai" if "gpt" in model else "anthropic"})
            llm_span = t.end(key, output=out)
            pending = []
            if m.get("content"):
                final_text = m["content"]
            for j, tc in enumerate(tcs):
                name, args = tc["function"]["name"], _args(tc["function"]["arguments"])
                errs = validate_args(args, schema.get(name)) if isinstance(args, dict) else ["arguments are not JSON"]
                # Some providers reuse ids ("0" on every turn), so the key is position-based and
                # results are matched to the oldest open call with that id.
                key = f"m{i}c{j}"
                t.start(key, f"execute_tool {name}", "tool", parent="agent", input=args,
                        attrs={"gen_ai.tool.name": name, "tdn.tool.args_valid": not errs,
                               "tdn.tool.validation_errors": errs or None,
                               "tdn.tau.call_id": key, "tdn.tau.emitted_by_step": llm_span.attributes["tdn.step"]})
                open_calls.setdefault(str(tc.get("id")), []).append(key)
                calls.append({"name": name, "args": args, "call_id": key})
            continue
        if role == "tool":
            cid = str(m.get("tool_call_id"))
            content = m.get("content")
            if open_calls.get(cid):
                key = open_calls[cid].pop(0)
                err = content if isinstance(content, str) and content.strip().lower().startswith("error") else None
                t.end(key, output=None if err else _content(content), error=err)
            pending.append({"role": "tool", "name": m.get("name"), "content": content})
    t.end("agent")
    reward = record.get("reward")
    t.finish(output=final_text, error=f"τ-bench reward {reward}: the task was not completed correctly")
    spans = mem.trace(t.trace_id)
    return t.trace_id, {"spans": spans, "blobs": dict(mem.blobs)}, calls


def label_step(label: Dict[str, Any], spans: List[Dict[str, Any]]) -> Optional[int]:
    if label["call_id"] is None:
        return None
    if label["call_id"] == "__final__":
        llm = [s for s in spans if s["kind"] == "llm"]
        return max(s["attributes"]["tdn.step"] for s in llm) if llm else None
    for s in spans:
        if s["attributes"].get("tdn.tau.call_id") == label["call_id"]:
            return s["attributes"]["tdn.step"]
    return None


def convert_records(records: List[Dict[str, Any]], domain: str, model: str, id_prefix: str) -> List[Dict[str, Any]]:
    """Convert τ-bench result records (historical or from a live ``tau_bench.run``) to cases."""
    specs = tool_specs(domain)
    out = []
    for rec in records:
        if rec.get("reward", 1) >= 1:
            continue
        info = rec.get("info") or {}
        expected = (info.get("task") or {}).get("actions") or []
        tid, bundle, calls = build_trace(rec, domain, model, specs)
        label = reference_label(calls, expected, info.get("reward_info") or {})
        label["step"] = label_step(label, bundle["spans"])
        emitted = None
        if label["step"] is not None:
            sp = next(s for s in bundle["spans"] if s["attributes"]["tdn.step"] == label["step"])
            emitted = sp["attributes"].get("tdn.tau.emitted_by_step")
        label["emitted_by_step"] = emitted
        out.append({"id": f"{id_prefix}-{domain}-t{rec['task_id']}-r{rec.get('trial', 0)}", "model": model,
                    "domain": domain, "task_id": rec["task_id"], "trial": rec.get("trial"),
                    "instruction": (info.get("task") or {}).get("instruction"),
                    "label": label, "bundle": bundle})
    return out


def convert(tau_dir: str, split: str) -> List[Dict[str, Any]]:
    prefix = SPLITS[split]
    out = []
    for domain in ("airline", "retail"):
        path = os.path.join(tau_dir, "historical_trajectories", f"{prefix}-{domain}.json")
        out += convert_records(json.load(open(path)), domain, MODELS[prefix], split)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tau-bench", required=True, help="path to a clone of sierra-research/tau-bench")
    args = ap.parse_args()
    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    for split in ("dev", "test"):
        cases = convert(args.tau_bench, split)
        with open(os.path.join(HERE, "data", f"{split}.jsonl"), "w") as f:
            for c in cases:
                f.write(json.dumps(c) + "\n")
        from collections import Counter
        print(split, len(cases), dict(Counter(c["label"]["category"] for c in cases)))


if __name__ == "__main__":
    main()
