"""Live harness 1: the OpenAI Agents SDK customer-service example, traced by Tracedown.

The agents (triage → FAQ / seat booking, with handoffs both ways) and tools are imported
unchanged from ``openai-agents-python/examples/customer_service/main.py``. The harness
plays the customer from a fixed script, checks each conversation against its expected
outcome, and labels failures with the same rule as τ-bench (LABELING.md):

* expected state change = the ``update_seat`` calls the conversation should produce;
* first write that does not match → ``called_wrong_tool`` / ``used_wrong_tool_argument``;
* a missing write → ``goal_partially_completed``; all writes right but the reply lacks the
  required fact → ``missing_output`` at the last model turn.

    git clone https://github.com/openai/openai-agents-python ../openai-agents-python
    pip install openai-agents
    export OPENAI_API_KEY=...
    python realworld/live/openai_agents_customer_service.py --agents-repo ../openai-agents-python \\
        --model gpt-4.1-mini --repeats 3
    python realworld/score.py --file realworld/data/live-oa-cs-gpt-4.1-mini.jsonl

``--fake`` swaps the LLM for a small scripted policy (no key, no network) with two
deliberate bugs, to check the plumbing end to end.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from typing import Any, Dict, List

from common import ROOT, exporter, write_cases

sys.path.insert(0, os.path.join(ROOT, "realworld"))
from tau_bench_import import reference_label  # noqa: E402

WRITES = {"update_seat"}
TOOLS = [{"name": "faq_lookup_tool", "side_effects": False}, {"name": "update_seat", "side_effects": True}]

# Each scenario: customer turns, expected update_seat calls, facts the final reply must contain.
SCENARIOS: List[Dict[str, Any]] = [
    {"id": "seat-simple", "turns": ["Hi, I'd like to change my seat. My confirmation number is ABC123 and I want seat 12A."],
     "expect": [{"confirmation_number": "ABC123", "new_seat": "12A"}], "reply": ["12A"]},
    {"id": "seat-two-turns", "turns": ["Can I change my seat please?", "Confirmation is QX77LM.", "I'd like 23C."],
     "expect": [{"confirmation_number": "QX77LM", "new_seat": "23C"}], "reply": ["23C"]},
    {"id": "seat-from-to", "turns": ["My confirmation is ZK40PA. Please move me from 14C to 7B."],
     "expect": [{"confirmation_number": "ZK40PA", "new_seat": "7B"}], "reply": ["7B"]},
    {"id": "seat-correction", "turns": ["Change my seat to 9D, confirmation number RT55YH.",
                                         "Sorry, I meant 9F, not 9D."],
     "expect": [{"confirmation_number": "RT55YH", "new_seat": "9D"}, {"confirmation_number": "RT55YH", "new_seat": "9F"}],
     "reply": ["9F"]},
    {"id": "faq-bags", "turns": ["How much can my carry-on bag weigh?"], "expect": [], "reply": ["50"]},
    {"id": "faq-seats", "turns": ["How many seats are on the plane?"], "expect": [], "reply": ["120"]},
    {"id": "faq-wifi", "turns": ["Is there wifi on board? What network do I join?"], "expect": [], "reply": ["Airline-Wifi"]},
    {"id": "faq-exit-rows", "turns": ["Which rows are the exit rows?"], "expect": [], "reply": ["4", "16"]},
    {"id": "faq-then-seat", "turns": ["Which rows are Economy Plus?",
                                      "Great, move me to 6A then. Confirmation MM12AB."],
     "expect": [{"confirmation_number": "MM12AB", "new_seat": "6A"}], "reply": ["6A"]},
    {"id": "no-write-question", "turns": ["My confirmation is PL09OK. Is seat 4C an exit row? Don't change anything yet."],
     "expect": [], "reply": ["exit"]},
    {"id": "out-of-scope", "turns": ["Please cancel my flight, confirmation HG33DS."], "expect": [], "reply": []},
    {"id": "unknown-faq", "turns": ["Do you serve vegetarian meals?"], "expect": [], "reply": []},
]


# ----------------------------------------------------------------------------- scripted model (--fake)

def _text(item: Any) -> str:
    c = item.get("content") if isinstance(item, dict) else getattr(item, "content", None)
    if isinstance(c, list):
        return " ".join((p.get("text") if isinstance(p, dict) else getattr(p, "text", "")) or "" for p in c)
    return c or ""


def make_fake_model():
    """A deterministic stand-in for the LLM. Bugs on purpose: it takes the *first* seat it
    sees in the conversation, and it treats any message with "seat" in it as a seat change."""
    from agents.items import ModelResponse
    from agents.models.interface import Model
    from agents.usage import Usage
    from openai.types.responses import ResponseFunctionToolCall, ResponseOutputMessage, ResponseOutputText

    counter = {"n": 0}

    def nid(p):
        counter["n"] += 1
        return f"{p}_{counter['n']}"

    def call(name, args):
        return ResponseFunctionToolCall(arguments=json.dumps(args), call_id=nid("call"), name=name,
                                        type="function_call", id=nid("fc"), status="completed")

    def say(text):
        return ResponseOutputMessage(id=nid("msg"), content=[ResponseOutputText(annotations=[], text=text, type="output_text")],
                                     role="assistant", status="completed", type="message")

    class ScriptedModel(Model):
        async def get_response(self, system_instructions, input, model_settings, tools, output_schema, handoffs,
                               tracing, *, previous_response_id=None, conversation_id=None, prompt=None, **kw):
            items = [{"role": "user", "content": input}] if isinstance(input, str) else list(input)

            def get(i, k):
                return i.get(k) if isinstance(i, dict) else getattr(i, k, None)
            users = [_text(i) for i in items if get(i, "role") == "user"]
            # only the turns since the agent last spoke to the customer
            last_reply = max([n for n, i in enumerate(items) if get(i, "role") == "assistant"] or [-1])
            new_users = [_text(i) for i in items[last_reply + 1:] if get(i, "role") == "user"]
            last = items[-1]
            last_out = get(last, "output") if get(last, "type") == "function_call_output" else None
            names = {get(i, "call_id"): get(i, "name") for i in items if get(i, "type") == "function_call"}
            last_tool = names.get(get(last, "call_id")) if last_out is not None else None
            sys_ = system_instructions or ""
            conv = " ".join(users)
            out: List[Any]
            if "triaging agent" in sys_:
                if last_out is not None and not new_users:
                    out = [say("Is there anything else I can help with?")]
                elif "seat" in " ".join(new_users).lower() and not re.search(r"how many|which|exit|economy", " ".join(new_users), re.I) \
                        or re.search(r"\bmove me\b|\bchange\b|\bmeant\b|\bcancel\b", " ".join(new_users), re.I):
                    out = [call("transfer_to_seat_booking_agent", {})]
                else:
                    out = [call("transfer_to_faq_agent", {})]
            elif "FAQ agent" in sys_:
                if last_tool == "faq_lookup_tool":
                    out = [say(str(last_out))]
                elif "move me" in " ".join(new_users).lower():
                    out = [call("transfer_to_triage_agent", {})]
                else:
                    out = [call("faq_lookup_tool", {"question": new_users[-1] if new_users else conv})]
            else:  # seat booking agent
                conf = re.findall(r"\b[A-Z]{2}\d{2}[A-Z]{2}\b|\b[A-Z]{3}\d{3}\b", conv)
                seats = re.findall(r"\b\d{1,2}[A-F]\b", conv)
                if last_tool == "update_seat":
                    out = [say(str(last_out) + ". Anything else?")]
                elif conf and seats:
                    out = [call("update_seat", {"confirmation_number": conf[0], "new_seat": seats[0]})]  # bug: first seat
                elif not conf:
                    out = [say("Sure. What is your confirmation number?")]
                else:
                    out = [say("Which seat would you like?")]
            # real models open a generation/response span themselves; do the same so the trace has LLM steps
            from agents.tracing import generation_span
            with generation_span(input=[i if isinstance(i, dict) else i.model_dump() for i in items], model="scripted",
                                 output=[o.model_dump() for o in out],
                                 usage={"input_tokens": 100, "output_tokens": 20}):
                pass
            return ModelResponse(output=out, usage=Usage(requests=1, input_tokens=100, output_tokens=20,
                                                         total_tokens=120), response_id=nid("resp"))

        def stream_response(self, *a, **kw):  # pragma: no cover
            raise NotImplementedError

    return ScriptedModel()


# ----------------------------------------------------------------------------- run one conversation

async def converse(cs, sc: Dict[str, Any], run_config) -> Dict[str, Any]:
    from agents import Runner, trace
    current, items, ctx = cs.triage_agent, [], cs.AirlineAgentContext()
    error = None
    with trace(f"Customer service {sc['id']}"):
        for turn in sc["turns"]:
            items.append({"content": turn, "role": "user"})
            try:
                result = await Runner.run(current, items, context=ctx, run_config=run_config, max_turns=12)
            except Exception as e:  # max turns, tool crash, model error: all are failures
                error = f"{type(e).__name__}: {e}"
                break
            items, current = result.to_input_list(), result.last_agent
            final = result.final_output
    return {"final": None if error else str(final), "error": error}


def judge_outcome(sc, spans, blobs, outcome) -> Dict[str, Any]:
    tools = sorted((s for s in spans if s["kind"] == "tool"), key=lambda s: s["attributes"]["tdn.step"])
    calls = [{"name": s["attributes"].get("gen_ai.tool.name"), "call_id": s["span_id"],
              "args": json.loads(blobs[s["input_digest"]]) if s.get("input_digest") in blobs else {}} for s in tools]
    calls = [dict(c, args=json.loads(c["args"]) if isinstance(c["args"], str) else c["args"]) for c in calls]
    made = [c["args"] for c in calls if c["name"] in WRITES]
    want = [dict(e) for e in sc["expect"]]
    reply = (outcome["final"] or "").lower()
    ok = outcome["error"] is None and made == want and all(f.lower() in reply for f in sc["reply"])
    if ok:
        return {"success": True, "label": None}
    expected = [{"name": "update_seat", "kwargs": e} for e in want]
    label = reference_label(calls, expected, {}, write=WRITES.__contains__)
    llm = sorted((s for s in spans if s["kind"] == "llm"), key=lambda s: s["attributes"]["tdn.step"])
    if label["call_id"] == "__final__":
        label["step"] = llm[-1]["attributes"]["tdn.step"] if llm else None
        label["detail"] = f"reply lacks {sc['reply']}" if outcome["error"] is None else outcome["error"]
    elif label["call_id"]:
        sp = next(s for s in spans if s["span_id"] == label["call_id"])
        label["step"] = sp["attributes"]["tdn.step"]
    else:
        label["step"] = None
    # the model turn that emitted the call also counts (as in τ-bench)
    label["emitted_by_step"] = None
    if label["step"] is not None:
        before = [s for s in llm if s["attributes"]["tdn.step"] < label["step"]]
        label["emitted_by_step"] = before[-1]["attributes"]["tdn.step"] if before else None
    return {"success": False, "label": label}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents-repo", required=True, help="clone of openai/openai-agents-python")
    ap.add_argument("--model", default="gpt-4.1-mini")
    ap.add_argument("--fake", action="store_true", help="scripted model; no API key needed")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--endpoint", help="also send runs to a Tracedown server, e.g. http://localhost:8000")
    ap.add_argument("--api-key")
    a = ap.parse_args()

    sys.path.insert(0, os.path.abspath(a.agents_repo))
    from agents import RunConfig
    from agent_tracedown.integrations import openai_agents as tdn_oa
    from examples.customer_service import main as cs

    mem = exporter(a.endpoint, a.api_key, project="oa-customer-service")
    proc = tdn_oa.instrument(replace=True)
    model = make_fake_model() if a.fake else a.model
    name = "fake" if a.fake else a.model.replace("/", "_")
    rc = RunConfig(model=model, tracing_disabled=False)

    cases = []
    for r in range(a.repeats):
        for sc in SCENARIOS:
            n_before = len(proc.trace_ids)
            outcome = asyncio.run(converse(cs, sc, rc))
            tid = proc.trace_ids[n_before]
            spans, blobs = mem.trace(tid), dict(mem.blobs)
            res = judge_outcome(sc, spans, blobs, outcome)
            root = next(s for s in spans if s["attributes"].get("tdn.run.root"))
            root["attributes"]["tdn.tools.available"] = TOOLS
            if not res["success"]:  # the SDK does not know the conversation missed its goal; we do
                root["status"] = {"code": "ERROR", "message": outcome["error"] or "conversation did not reach its goal"}
                root["attributes"]["tdn.run.outcome"] = "failure"
            used = {d for s in spans for d in (s.get("input_digest"), s.get("output_digest")) if d}
            bundle = {"spans": spans, "blobs": {d: blobs[d] for d in used if d in blobs}, "write_tools": sorted(WRITES)}
            cases.append({"id": f"oa-cs-{name}-{sc['id']}-r{r}", "model": name, "scenario": sc["id"],
                          "success": res["success"], "label": res["label"], "bundle": bundle})
            print(f"{sc['id']:<20} {'ok' if res['success'] else 'FAIL ' + res['label']['category'] + '@' + str(res['label']['step'])}")
    write_cases(os.path.join(ROOT, "realworld", "data", f"live-oa-cs-{name}.jsonl"), cases)


if __name__ == "__main__":
    main()
