"""Convert Who&When failure logs (Zhang et al., ICML 2025) into Tracedown traces.

    git clone https://github.com/mingyin1/Agents_Failure_Attribution ../Agents_Failure_Attribution
    python realworld/whowhen/convert.py --repo ../Agents_Failure_Attribution

Every history entry becomes one or two spans that carry ``tdn.ww.index`` (the Who&When
step, 0-based history index) and ``tdn.ww.agent`` (the agent name used by the labels):

* an agent's message → an agent span with one LLM span (its text is the output; the
  previous entry is the input, as a user message, so claims have evidence);
* code-execution output (``Computer_terminal`` / ``ComputerTerminal``) → a tool span under
  the agent that asked for it, marked as an error when the output shows a failure;
* Magentic-One's acting agents (``WebSurfer``, ``FileSurfer``) → an agent span with a tool
  span (``execute_tool web_surfer``), since each of their turns reports an action taken;
* ``human`` → the request (run input).

Each run ends failed ("the final answer is wrong"); with ``--with-ground-truth`` the
expected answer is added to the failure message, matching the paper's two settings.
Writes ``realworld/data/whowhen-{subset}[-gt].jsonl`` (gitignored).
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from typing import Any, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path[:0] = [os.path.join(ROOT, "sdk", "python")]
import agent_tracedown as tdn  # noqa: E402

SUBSETS = {"algorithm": "Algorithm-Generated", "hand": "Hand-Crafted"}
TERMINALS = {"Computer_terminal", "ComputerTerminal"}
ACTORS = {"WebSurfer": "web_surfer", "FileSurfer": "file_surfer"}
# Failure markers in code output / browsing reports (not tuned on labels).
_FAIL = re.compile(r"exitcode: [1-9]|execution failed|Traceback \(most recent call last\)|\b\w*Error:|"
                   r"\bnot found\b|\bfailed to\b|\bunable to\b|\bcould not\b|\btimed? ?out\b", re.I)


def agent_of(entry: Dict[str, Any], subset: str) -> str:
    """The agent name as the labels use it."""
    if subset == "algorithm":
        return entry.get("name") or entry.get("role") or "unknown"
    role = entry.get("role", "")
    return role.split(" (")[0].strip()          # "Orchestrator (-> WebSurfer)" → "Orchestrator"


def is_failure_output(text: str) -> bool:
    return bool(_FAIL.search(text or ""))


def build(record: Dict[str, Any], subset: str, with_gt: bool) -> Dict[str, Any]:
    mem = tdn.MemoryExporter()
    tdn.init(project="whowhen", exporter=mem)
    hist = record["history"]
    t = tdn.ManualTrace("Who&When " + SUBSETS[subset], input=record.get("question"))
    tools = set()
    last_agent_key, prev_text = None, record.get("question")
    for i, e in enumerate(hist):
        text = e.get("content") or ""
        who = agent_of(e, subset)
        tag = {"tdn.ww.index": i, "tdn.ww.agent": who}
        if subset == "hand" and who == "human":
            prev_text = text
            continue
        if who in TERMINALS:
            parent = last_agent_key
            k = f"t{i}"
            t.start(k, "execute_tool run_code", "tool", parent=parent, input={"code": "(see previous message)"},
                    attrs={"gen_ai.tool.name": "run_code", **tag})
            t.end(k, output=text[:4000], error=("code execution failed: " + text[:300]) if is_failure_output(text) else None)
            tools.add("run_code")
        elif who in ACTORS:
            a, k = f"a{i}", f"t{i}"
            t.start(a, f"invoke_agent {who}", "agent", attrs=tag)
            t.start(k, f"execute_tool {ACTORS[who]}", "tool", parent=a, input={"instruction": prev_text[:2000]},
                    attrs={"gen_ai.tool.name": ACTORS[who], **tag})
            t.end(k, output=text[:4000], error=("action failed: " + text[:300]) if is_failure_output(text) else None)
            t.end(a)
            tools.add(ACTORS[who])
        else:
            a, k = f"a{i}", f"l{i}"
            t.start(a, f"invoke_agent {who}", "agent", attrs=tag)
            t.start(k, "chat", "llm", parent=a, input=[{"role": "user", "content": prev_text[:4000]}],
                    attrs={"gen_ai.request.model": "unknown", **tag})
            t.end(k, output={"text": text[:6000]})
            t.end(a)
            last_agent_key = a
        prev_text = text
    t.declare_tools([{"name": n, "side_effects": False} for n in sorted(tools)])
    msg = "final answer is wrong"
    if with_gt:
        msg += f"; expected: {record.get('ground_truth')}"
    tid = t.finish(output=(hist[-1].get("content") or "")[-2000:] if hist else None, error=msg)
    spans = mem.trace(tid)
    return {"spans": spans, "blobs": dict(mem.blobs)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True, help="clone of mingyin1/Agents_Failure_Attribution")
    a = ap.parse_args()
    out_dir = os.path.join(ROOT, "realworld", "data")
    os.makedirs(out_dir, exist_ok=True)
    for subset, folder in SUBSETS.items():
        files = sorted(glob.glob(os.path.join(a.repo, "Who&When", folder, "*.json")))
        for with_gt in (False, True):
            path = os.path.join(out_dir, f"whowhen-{subset}{'-gt' if with_gt else ''}.jsonl")
            with open(path, "w") as f:
                for p in files:
                    rec = json.load(open(p))
                    agents = sorted({agent_of(e, subset) for e in rec["history"]} - {"human"} - TERMINALS)
                    f.write(json.dumps({
                        "id": f"{subset}/{os.path.basename(p)}", "subset": subset,
                        "mistake_agent": str(rec["mistake_agent"]), "mistake_step": int(rec["mistake_step"]),
                        "n_steps": len(rec["history"]), "agents": agents,
                        "roles": [agent_of(e, subset) for e in rec["history"]],
                        "bundle": build(rec, subset, with_gt)}) + "\n")
            print(f"{folder}{' (with ground truth)' if with_gt else ''}: {len(files)} logs → {path}")


if __name__ == "__main__":
    main()
