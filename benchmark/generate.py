"""TDN-RCA-Bench: generate labelled failing agent runs.

Each case is a recorded run (spans + payload blobs, the same bundle the replay API
returns) plus a label: the root-cause category and the step where the cause is.
Scenarios cover ten failure families. Most cases add **distractors**: a harmless
issue elsewhere in the run (a retried transient error, an unused low-score chunk,
an extra tool call) so that "blame the first error" or "blame the last error" is
not enough.

Three splits come from different seeds and wordings: ``dev`` for development,
``test`` (used once, then for fixes in v0) and ``holdout`` (new wordings and number
formats, never used for tuning). Report on ``holdout``.

    python benchmark/generate.py            # writes benchmark/data/{dev,test,holdout}.jsonl
"""
from __future__ import annotations

import json
import os
import random
import sys
from typing import Any, Callable, Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "sdk", "python"))

import agent_tracedown as tdn  # noqa: E402

# Domain wordings; "test" adds unseen ones.
DOMAINS = {
    "dev": {
        "agents": ["Planner", "Router", "Assistant", "Coordinator"],
        "dates": [("next Friday at 3pm", "Friday"), ("tomorrow morning", "tomorrow"), ("end of month", "EOM")],
        "kb": [("refund-policy", "refunds within 30 days are $120", "refunds within 30 days are $80")],
        "tools": [("search_flights", "find flights between two airports"), ("book_hotel", "reserve a hotel room")],
    },
    "test": {
        "agents": ["Dispatcher", "Orchestrator", "Concierge", "Supervisor"],
        "dates": [("the second Tuesday", "Tuesday"), ("in two weeks", "two weeks"), ("Q3 close", "Q3")],
        "kb": [("shipping-sla", "standard shipping takes 7 days", "standard shipping takes 3 days"),
               ("warranty", "the warranty lasts 12 months", "the warranty lasts 24 months")],
        "tools": [("create_ticket", "open a support ticket"), ("reset_password", "reset a user's password")],
    },
    # Written after the test split had been used for fixes: the only split no change was tuned on.
    "holdout": {
        "agents": ["Navigator", "Steward", "Analyst", "Liaison"],
        "dates": [("after the board meeting", "after board"), ("first business day of May", "1st BD"),
                  ("noon-ish Thursday", "Thu noon")],
        "kb": [("data-retention", "logs are kept for 90 days", "logs are kept for 30 days"),
               ("overtime-policy", "overtime is paid at 1.5x", "overtime is paid at 2x")],
        "tools": [("transfer_funds", "move money between two accounts"),
                  ("schedule_pickup", "arrange a courier pickup for a parcel")],
        "prices": [(1200, 1250), (0.99, 1.99), (15, 51)],
    },
}


def _fake(text: Any, tin: int = 120, tout: int = 20) -> Callable[..., Dict[str, Any]]:
    return lambda p, **_: {"text": text if isinstance(text, str) else json.dumps(text),
                           "usage": {"input_tokens": tin, "output_tokens": tout}}


def _noise(r: random.Random, agent: str) -> None:
    """Harmless steps that appear in healthy runs too."""
    if r.random() < 0.5:
        tdn.memory_read("user.locale", "en-US")
    if r.random() < 0.4:
        tdn.tool_call("get_user_profile", {"user": f"u{r.randint(1, 99)}"}, lambda user: {"tier": "pro"})


def _transient_retry(r: random.Random) -> None:
    """Distractor: a tool fails once with 503 and succeeds on retry."""
    try:
        tdn.tool_call("rate_api", {"pair": "USD/EUR"}, lambda pair: (_ for _ in ()).throw(RuntimeError("503 Service Unavailable")))
    except RuntimeError:
        pass
    tdn.tool_call("rate_api", {"pair": "USD/EUR"}, lambda pair: {"rate": 0.92})


def _unused_weak_retrieval(r: random.Random) -> None:
    """Distractor: a low-score retrieval whose chunks never reach the answer."""
    tdn.retrieval("unrelated faq", lambda q: [{"id": "faq-9", "text": "Office hours are 9 to 5.", "score": 0.31,
                                               "source": "faq.md"}], source="kb")


DISTRACTORS = [_transient_retry, _unused_weak_retrieval]


def _step_of(mem: tdn.MemoryExporter, tid: str, name: str, nth: int = 0) -> int:
    steps = sorted(s["attributes"]["tdn.step"] for s in mem.trace(tid) if s["name"] == name)
    return steps[nth]


# ----------------------------------------------------------------------------- scenario families
# Each returns (category, root step name, nth occurrence).

def bad_llm_output(r, d, distract):
    phrase, bad = r.choice(d["dates"])
    agent = r.choice(d["agents"])
    schema = {"required": ["start"], "properties": {"start": {"type": "string", "format": "date-time"}}}
    with tdn.run("SchedulingAgent", input=f"set up a review {phrase}") as run:
        with tdn.agent(agent):
            _noise(r, agent)
            if distract:
                r.choice(DISTRACTORS)(r)
            out = tdn.llm_call(_fake({"start": bad}), prompt=f"extract start from: {phrase}", model="model-a")
            try:
                tdn.tool_call("create_event", json.loads(out["text"]), lambda start: {"id": 1}, schema=schema)
            except tdn.ToolError:
                pass
        run.fail("event was not created")
    return "bad_llm_output", "chat model-a", -1


def wrong_tool(r, d, distract):
    (right, rdesc), (wrong, wdesc) = r.sample(d["tools"], 2)
    tools = [{"name": right, "description": rdesc, "schema": {"required": ["ref"], "additionalProperties": False,
                                                              "properties": {"ref": {"type": "string"}}}},
             {"name": wrong, "description": wdesc, "schema": {"required": ["code"], "additionalProperties": False,
                                                              "properties": {"code": {"type": "string"}}}}]
    with tdn.run("ToolAgent", input=f"please {rdesc}") as run:
        run.tools(tools)
        with tdn.agent(r.choice(d["agents"])):
            if distract:
                r.choice(DISTRACTORS)(r)
            tdn.llm_call(_fake({"tool": wrong}), prompt="pick a tool", model="model-a")
            try:
                tdn.tool_call(wrong, {"ref": "R-1"}, lambda **k: None, schema=tools[1]["schema"])
            except tdn.ToolError:
                pass
        run.fail("request not completed")
    return "wrong_tool", f"execute_tool {wrong}", 0


def stale_source(r, d, distract):
    doc, old, new = r.choice(d["kb"])
    with tdn.run("KBAgent", input=f"what does the {doc} say?") as run:
        with tdn.agent(r.choice(d["agents"])):
            _noise(r, "kb")
            chunks = tdn.retrieval(doc, lambda q: [
                {"id": f"{doc}#old", "text": old.capitalize() + ".", "source": f"{doc}-2023.md", "version": "2023",
                 "score": 0.9, "stale": True},
                {"id": "misc#1", "text": "Contact support for exceptions.", "source": "misc.md", "score": 0.4}],
                source="kb")
            if distract:
                _transient_retry(r)
            ans = tdn.llm_call(_fake(f"According to policy, {old}."), prompt={"ctx": [c["text"] for c in chunks]},
                               model="model-b")
        run.output(ans["text"])
        run.fail("customer received outdated information")
    return "stale_source", "retrieval", 0


def retrieval_failure(r, d, distract):
    empty = r.random() < 0.5
    with tdn.run("KBAgent", input="how do I export my data?") as run:
        with tdn.agent(r.choice(d["agents"])):
            if distract:
                _transient_retry(r)
            chunks = tdn.retrieval("export data", lambda q: [] if empty else [
                {"id": "x", "text": "Our offices are closed on holidays.", "score": 0.22, "source": "misc.md"}],
                source="kb")
            ans = tdn.llm_call(_fake("Go to Settings and click Export."), prompt={"ctx": chunks}, model="model-b")
        run.output(ans["text"])
        run.fail("instructions were wrong")
    return "retrieval_failure", "retrieval", 0


def loop(r, d, distract):
    n = r.randint(3, 6)
    with tdn.run("ResearchAgent", input="find the latest SDK version", step_budget=n + 2) as run:
        with tdn.agent(r.choice(d["agents"])):
            if distract:
                _unused_weak_retrieval(r)
            for _ in range(n):
                tdn.llm_call(_fake("search again"), prompt="find it", model="model-a")
                tdn.tool_call("web_search", {"q": "sdk latest version"}, lambda q: {"results": []})
        run.output("not found")
        run.fail("no answer")
    return "loop", "execute_tool web_search", 0


def context_window(r, d, distract):
    with tdn.run("SummaryAgent", input="summarize the thread") as run:
        with tdn.agent(r.choice(d["agents"])):
            if distract:
                _transient_retry(r)
            tdn.llm_call(_fake("Summary of the first part only.", tin=127_500), prompt="(very long thread)",
                         model="model-c", context_limit=128_000)
        run.output("Summary of the first part only.")
        run.fail("summary missed the conclusion")
    return "context_window", "chat model-c", 0


def hallucination(r, d, distract):
    true_v, said = r.choice(d.get("prices") or [(39, 49), (12, 21), (250, 205), (7, 9)])
    with tdn.run("SalesAgent", input="what does the pro plan cost?") as run:
        with tdn.agent(r.choice(d["agents"])):
            _noise(r, "sales")
            if distract:
                _unused_weak_retrieval(r)
            p = tdn.tool_call("get_price", {"plan": "pro"}, lambda plan: {"plan": "pro", "usd_per_month": true_v})
            ans = tdn.llm_call(_fake(f"The pro plan costs ${said} per month."), prompt={"price": p}, model="model-a")
        run.output(ans["text"])
        run.fail("customer was quoted the wrong price")
    return "hallucination", "chat model-a", -1


def memory_corruption(r, d, distract):
    with tdn.run("CartAgent", input="check out my cart") as run:
        with tdn.agent(r.choice(d["agents"])):
            tdn.memory_write("cart.items", ["sku-1", "sku-2"])
            if distract:
                _transient_retry(r)
            items = tdn.memory_read("cart.items", ["sku-1"])
            ans = tdn.llm_call(_fake(f"You are buying {len(items)} item."), prompt={"items": items}, model="model-a")
        run.output(ans["text"])
        run.fail("an item disappeared from the order")
    return "memory_corruption", "memory read cart.items", 0


def planning_failure(r, d, distract):
    with tdn.run("TravelAgent", input="book the 9am flight if there is a seat") as run:
        with tdn.agent(r.choice(d["agents"])):
            seats = tdn.tool_call("check_seats", {"flight": "AB12"}, lambda flight: {"available": 0})
            if distract:
                _unused_weak_retrieval(r)
            tdn.decision(["book", "waitlist", "ask_user"], "book", rationale="user asked for 9am",
                         precondition_ok=seats["available"] > 0)
            tdn.tool_call("book_flight", {"flight": "AB12"}, lambda flight: {"status": "overbooked"})
        run.output("Booked AB12.")
        run.fail("customer was overbooked")
    return "planning_failure", "decision book", 0


def tool_failure(r, d, distract):
    with tdn.run("OpsAgent", input="restart the payments service") as run:
        with tdn.agent(r.choice(d["agents"])):
            _noise(r, "ops")
            if distract:
                _unused_weak_retrieval(r)
            out = tdn.llm_call(_fake({"service": "payments"}), prompt="which service?", model="model-a")
            try:
                tdn.tool_call("restart_service", json.loads(out["text"]),
                              lambda service: (_ for _ in ()).throw(RuntimeError("500 orchestrator unavailable")))
            except RuntimeError:
                pass
        run.fail("service not restarted")
    return "tool_failure", "execute_tool restart_service", 0


FAMILIES = [bad_llm_output, wrong_tool, stale_source, retrieval_failure, loop, context_window,
            hallucination, memory_corruption, planning_failure, tool_failure]


def generate(split: str, per_family: int, seed: int) -> List[Dict[str, Any]]:
    r = random.Random(seed)
    d = DOMAINS[split]
    cases = []
    for fam in FAMILIES:
        for i in range(per_family):
            mem = tdn.MemoryExporter()
            tdn.init(project="bench", exporter=mem)
            distract = i % 3 != 0  # two thirds of cases carry a distractor
            category, step_name, nth = fam(r, d, distract)
            tid = mem.spans[-1]["trace_id"]
            spans = mem.trace(tid)
            cases.append({
                "id": f"{split}-{fam.__name__}-{i:02d}",
                "family": fam.__name__, "distractor": distract,
                "label": {"category": category, "step": _step_of(mem, tid, step_name, nth), "span_name": step_name},
                "bundle": {"spans": spans, "blobs": mem.blobs},
            })
    return cases


def main() -> None:
    out = os.path.join(HERE, "data")
    os.makedirs(out, exist_ok=True)
    for split, seed, n in (("dev", 1, 8), ("test", 2, 12), ("holdout", 3, 10)):
        cases = generate(split, n, seed)
        with open(os.path.join(out, f"{split}.jsonl"), "w") as f:
            for c in cases:
                f.write(json.dumps(c) + "\n")
        print(f"{split}: {len(cases)} cases")


if __name__ == "__main__":
    main()
