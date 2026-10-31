"""Who is at fault? The agent, the user, or nobody yet.

Not every failed run is an agent bug. Benchmarks and production both count runs as
failed when the user gave details that match no account, turned down what the agent
offered, or simply left while the agent was waiting for an answer. Debugging those as
agent errors wastes time. For a failed run this module returns one verdict:

* ``agent``      the agent did something wrong (the root-cause findings say where);
* ``user``       the run failed because of the user: their details never matched an
                 account, or they declined what the agent offered and nothing else changed;
* ``unfinished`` nothing went wrong yet: the run stopped while the agent was waiting for
                 the user, with no error and no unsupported claim.

The rules use only the trace: user messages, tool calls and their results, and the
agent's last turn. They make no use of any benchmark's expected answers.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .detectors import RunView, has_side_effects, text_of

# Tools that identify the user or their account: find_user_id_by_email, get_user_details,
# lookup_customer, verify_account, authenticate_member ...
_IDENTITY = re.compile(r"(user|customer|account|member|profile|identity|client)", re.I)
_LOOKUP = re.compile(r"^(find|get|lookup|look_up|search|verify|authenticate|identify|check|fetch|retrieve)", re.I)

# The user turning an offer down, or choosing to leave things as they are.
_DECLINE = re.compile(
    r"^\W*(no\b(?!,? (that's|that is) (all|it))|nope|no need|never ?mind|actually,? no\b|"
    r"(i'?ll|i will|i'?d rather|let'?s|i'?d prefer to) (just )?(keep|wait|leave|pass|hold off)|"
    r"(i )?(don'?t|do not) (want|need) (to|it|that|this|the)|i (have )?changed my mind|not interested|"
    r"i('?ve| have)? decided (not|against|to keep)|forget (it|about it))", re.I)
# ...said in answer to an offer or a question about doing something.
_OFFER = re.compile(r"(would you like|do you want|shall i|should i|can i proceed|do you wish|would you prefer|"
                    r"please confirm|confirm (that|if|whether)|(yes|no)\)?\s*[?.:]|proceed\b|options?\b)", re.I)

# The agent waiting on the user: a real question, not "anything else I can help with?".
_COURTESY_Q = re.compile(r"(anything else|help you with today|assist you (with anything|further)|other questions)", re.I)


def _llm_text(run: RunView, s) -> str:
    out = run.output(s)
    return text_of(out.get("text") if isinstance(out, dict) else out)


def _user_messages(run: RunView) -> List[Dict[str, Any]]:
    """[{step, text, before}] for each user message, in order, as the model saw them;
    ``before`` is what the agent said just before it."""
    out, seen, prev = [], set(), ""
    for s in sorted(run.of_kind("llm"), key=run.step):
        inp = run.input(s)
        if isinstance(inp, list):
            for m in inp:
                if isinstance(m, dict) and m.get("role") == "user":
                    t = text_of(m.get("content"))
                    if t and t not in seen:
                        seen.add(t)
                        out.append({"step": run.step(s), "text": t, "before": prev})
        prev = _llm_text(run, s) or prev
    req = run.input(run.root)
    if isinstance(req, str) and req not in seen:
        out.insert(0, {"step": 0, "text": req, "before": ""})
    return out


def _last_agent_text(run: RunView) -> str:
    llm = sorted(run.of_kind("llm"), key=run.step)
    for s in reversed(llm):
        out = run.output(s)
        t = out.get("text") if isinstance(out, dict) else out
        if t:
            return text_of(t)
    return text_of(run.output(run.root))


def _waiting_on_user(text: str) -> bool:
    """Does the agent's last turn end by asking the user for something?"""
    t = re.sub(r"<function_calls>.*", "", text or "", flags=re.S).strip()
    if not t:
        return False
    last = re.split(r"(?<=[.!?])\s+", t)[-1]
    if not last.endswith("?") or _COURTESY_Q.search(last):
        return False
    return True


def attribute_fault(run: RunView, findings: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Verdict for a failed run (None for a successful one)."""
    failed = run.root["status"]["code"] == "ERROR" or run.root["attributes"].get("tdn.run.outcome") == "failure"
    if not failed:
        return None
    declared = {t.get("name"): t for t in (run.root["attributes"].get("tdn.tools.available") or []) if isinstance(t, dict)}
    tools = sorted(run.of_kind("tool"), key=run.step)
    name = lambda s: s["attributes"].get("gen_ai.tool.name") or s["name"]  # noqa: E731
    ok = lambda s: s["status"]["code"] != "ERROR"  # noqa: E731
    writes = [s for s in tools if has_side_effects(s, declared)]
    ok_writes = [s for s in writes if ok(s)]
    identity = [s for s in tools if _IDENTITY.search(name(s)) and _LOOKUP.search(name(s))]
    users = _user_messages(run)
    agent_side = [f for f in findings if f["category"] not in ("reported_failure",)]
    top = agent_side[0] if agent_side else None

    # U1: the user's details never matched an account, so nothing could be done.
    failed_id = [s for s in identity if not ok(s)]
    if len(failed_id) >= 2 and not any(ok(s) for s in identity) and not ok_writes:
        return {"verdict": "user", "confidence": 0.8, "step": run.step(failed_id[0]),
                "reason": f"the user's details matched no account: {len(failed_id)} lookups "
                          f"({', '.join(sorted({name(s) for s in failed_id}))}) failed and none succeeded"}

    # U2 (hint only): the user turned down something the agent offered and nothing changed
    # afterwards. Measured right in 1 of 3 cases (often a "no" to a side question such as
    # extra bags), so it never decides the verdict on its own.
    hints = []
    for m in users:
        if _DECLINE.search(m["text"]) and _OFFER.search(m["before"] or ""):
            later_writes = [s for s in writes if run.step(s) >= m["step"]]
            if not later_writes and not any(not ok(s) for s in writes):
                hints.append(f'step {m["step"]}: the user declined something and nothing changed afterwards: '
                             f'"{m["text"][:100]}"')
                break

    # N1: the run stopped while the agent was waiting for the user, with nothing wrong so far.
    hard = [f for f in agent_side if f["category"] in ("hallucination", "tool_failure", "invalid_tool_args", "loop",
                                                        "wrong_tool", "planning_failure", "policy_violation")
            and f["confidence"] >= 0.5]
    if _waiting_on_user(_last_agent_text(run)) and not hard and not any(not ok(s) for s in writes):
        return {"verdict": "unfinished", "confidence": 0.55, "step": None, "hints": hints,
                "reason": "the run stopped while the agent was waiting for the user's answer; no error so far"}

    if top is None:
        return {"verdict": "unclear", "confidence": 0.3, "step": None, "hints": hints,
                "reason": "no evidence for either side"}
    return {"verdict": "agent", "confidence": round(min(0.9, max(0.4, top["confidence"])), 2), "step": top["step"],
            "hints": hints, "reason": f"{top['label']} at step {top['step']}"}


# ----------------------------------------------------------------------------- LLM judge

FAULT_PROMPT = """You are reviewing a failed conversation between an AI agent and a user (the user may be simulated).
Decide who caused the failure:
- "agent": the agent made a mistake (wrong action, wrong details, broke a rule, gave up, said something false)
- "user": the agent behaved reasonably, but the user caused the failure (gave wrong or unverifiable details,
  chose or confirmed the wrong option themselves, changed their mind, declined, or left)
- "nobody": no real mistake; the conversation simply stopped before the task could finish

Run transcript:
{transcript}

Answer with JSON only: {{"fault": "agent|user|nobody", "confidence": <0..1>, "explanation": "<one sentence>"}}"""


def judge_fault(run: RunView, judge) -> Optional[Dict[str, Any]]:
    """Ask an LLM judge (any ``callable(prompt) -> str``) who caused a failed run."""
    import json
    from .judge import transcript
    try:
        answer = judge(FAULT_PROMPT.format(transcript=transcript(run)))
        m = re.search(r"\{.*\}", answer or "", re.S)
        d = json.loads(m.group(0)) if m else {}
    except Exception:  # noqa: BLE001 - the judge is optional
        return None
    verdict = {"agent": "agent", "user": "user", "nobody": "unfinished"}.get(str(d.get("fault", "")).lower())
    if verdict is None:
        return None
    conf = max(0.0, min(float(d.get("confidence", 0.5) or 0.5), 1.0))
    return {"verdict": verdict, "confidence": round(conf, 2), "step": None, "hints": [],
            "reason": f"LLM judge: {str(d.get('explanation', ''))[:200]}", "source": "llm_judge"}
