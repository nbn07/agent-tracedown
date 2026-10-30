"""Four small agents that fail in realistic ways. No API keys needed: the
"models" are deterministic stand-ins, so every run reproduces exactly.

    python examples/run_demo.py --endpoint http://localhost:7070
"""
from __future__ import annotations

import json

import agent_tracedown as tdn

CALENDAR_SCHEMA = {
    "type": "object",
    "required": ["title", "start"],
    "properties": {"title": {"type": "string"}, "start": {"type": "string", "format": "date-time"}},
}


# ----------------------------------------------------------------------------- 1. wrong tool arguments

def extract_event_buggy(prompt, model, temperature=None, **_):
    """Simulates a model that copies the weekday instead of resolving it to a date."""
    return {"text": json.dumps({"title": "Call with Priya", "start": "Friday"}),
            "usage": {"input_tokens": 182, "output_tokens": 19}}


def extract_event_fixed(prompt, model, temperature=None, **_):
    """What a better prompt or model returns: an absolute ISO-8601 time."""
    return {"text": json.dumps({"title": "Call with Priya", "start": "2026-11-06T15:00:00-05:00"}),
            "usage": {"input_tokens": 201, "output_tokens": 27}}


def create_event(title, start):
    return {"event_id": "evt_8812", "title": title, "start": start, "status": "confirmed"}


def calendar_agent(message="Book a call with Priya next Friday at 3pm", extractor=extract_event_buggy):
    with tdn.run("CalendarAssistant", input=message, step_budget=12) as run:
        with tdn.agent("PlannerAgent", role="planner"):
            tz = tdn.memory_read("user.timezone", "America/New_York")
            tdn.decision(["create_event", "find_free_slot", "ask_clarifying_question"], "create_event",
                         rationale="user gave a day and a time")
            raw = tdn.llm_call(extractor, model="gpt-4o-mini", provider="openai", temperature=0.2,
                               prompt=[{"role": "system", "content": f"Extract the event. User timezone: {tz}."},
                                       {"role": "user", "content": message}],
                               prompt_version="extract-event@v3")
            event = json.loads(raw["text"])
            result = tdn.tool_call("calendar_create_event", event, create_event, schema=CALENDAR_SCHEMA)
        with tdn.agent("WriterAgent", role="writer"):
            tdn.handoff("PlannerAgent", "WriterAgent", result)
            reply = tdn.llm_call(lambda p, **_: {"text": f"Booked: {result['title']} on {result['start']}."},
                                 model="gpt-4o-mini", provider="openai", prompt=result)
        run.output(reply["text"])
        return run.trace_id


# ----------------------------------------------------------------------------- 2. outdated retrieval source

POLICY_CHUNKS = [
    {"id": "kb-refunds-2023#4", "source": "refund-policy-2023.md", "version": "2023.2", "score": 0.91, "stale": True,
     "text": "Annual plan cancellations within 30 days receive a refund of $120."},
    {"id": "kb-billing-faq#2", "source": "billing-faq.md", "version": "2026.1", "score": 0.62,
     "text": "Refunds are issued to the original payment method within 5 business days."},
]


def refund_agent(message="I cancelled my annual plan after 10 days. How much will I get back?"):
    with tdn.run("SupportBot", input=message) as run:
        with tdn.agent("ResearchAgent", role="research"):
            chunks = tdn.retrieval("annual plan refund amount", lambda q: POLICY_CHUNKS, source="help-center")
            tdn.handoff("ResearchAgent", "WriterAgent", [c["id"] for c in chunks])
        with tdn.agent("WriterAgent", role="writer"):
            context = "\n".join(c["text"] for c in chunks)
            answer = tdn.llm_call(
                lambda p, **_: {"text": "You will receive a refund of $120. Refunds are issued to the original "
                                        "payment method within 5 business days.",
                                "usage": {"input_tokens": 312, "output_tokens": 31}},
                model="claude-haiku-4-5", provider="anthropic",
                prompt={"system": "Answer only from context.", "context": context, "question": message})
        run.output(answer["text"])
        # Found later: the current policy pays $80. Feedback marks the run failed.
        run.fail("customer was quoted an incorrect refund amount")
        return run.trace_id


# ----------------------------------------------------------------------------- 3. loop

def research_loop_agent(message="What is the latest stable version of our internal SDK?"):
    with tdn.run("ResearchAssistant", input=message, step_budget=6) as run:
        with tdn.agent("ResearchAgent", role="research"):
            for _ in range(4):
                tdn.llm_call(lambda p, **_: {"text": "I should search again."}, model="gpt-4o-mini",
                             provider="openai", prompt=message)
                tdn.tool_call("web_search", {"query": "internal SDK latest version"},
                              lambda query: {"results": []})
        run.output("I could not find the version.")
        run.fail("step budget exceeded without an answer")
        return run.trace_id


# ----------------------------------------------------------------------------- 4. unsupported claim

def pricing_agent(message="How much is the Pro plan?"):
    with tdn.run("SalesAssistant", input=message) as run:
        with tdn.agent("PricingAgent"):
            price = tdn.tool_call("get_price", {"plan": "pro"}, lambda plan: {"plan": "pro", "usd_per_month": 39})
            answer = tdn.llm_call(
                lambda p, **_: {"text": "The Pro plan costs $49 per month and includes priority support."},
                model="gpt-4o-mini", provider="openai", prompt={"question": message, "price": price})
        run.output(answer["text"])
        return run.trace_id


ALL = {"calendar": calendar_agent, "refund": refund_agent, "loop": research_loop_agent, "pricing": pricing_agent}


# ----------------------------------------------------------------------------- 5. wrong tool

ACCOUNT_TOOLS = [
    {"name": "refund_order", "description": "Refund a single order that was charged by mistake",
     "schema": {"type": "object", "required": ["order_id"], "additionalProperties": False,
                "properties": {"order_id": {"type": "string"}, "amount": {"type": "number"}}}},
    {"name": "cancel_subscription", "description": "Cancel a recurring subscription plan for the customer",
     "schema": {"type": "object", "required": ["subscription_id"], "additionalProperties": False,
                "properties": {"subscription_id": {"type": "string"}}}},
]


def account_agent(message="Please cancel my subscription sub_981, I no longer need it."):
    with tdn.run("AccountAgent", input=message) as run:
        run.tools(ACCOUNT_TOOLS)
        with tdn.agent("RouterAgent"):
            pick = tdn.llm_call(lambda p, **_: {"text": json.dumps({"tool": "refund_order",
                                                                    "args": {"subscription_id": "sub_981"}})},
                                model="gpt-4o-mini", provider="openai", prompt=message)
            choice = json.loads(pick["text"])
            schema = next(t["schema"] for t in ACCOUNT_TOOLS if t["name"] == choice["tool"])
            tdn.tool_call(choice["tool"], choice["args"], lambda **kw: {"ok": True}, schema=schema)
        run.output("Done.")
        return run.trace_id


# ----------------------------------------------------------------------------- 6. drift: a prompt change

def support_cohort(prompt_version: str, i: int):
    """SupportBot answering billing questions. Prompt v4 shortens the system prompt and,
    as a side effect, the model starts calling the `escalate` tool and failing more."""
    import random
    rnd = random.Random(f"{prompt_version}-{i}")
    question = rnd.choice(["Why was I charged twice?", "Can I change my billing date?", "Where is my invoice?"])
    with tdn.run("SupportBot", input=question, release="2026.10." + ("1" if prompt_version == "billing@v3" else "2")) as run:
        with tdn.agent("BillingAgent"):
            tdn.llm_call(lambda p, **_: {"text": "lookup", "usage": {"input_tokens": 220 if prompt_version.endswith("v3") else 160,
                                                                     "output_tokens": 20}},
                         model="gpt-4o-mini", provider="openai", prompt=question, prompt_version=prompt_version)
            tdn.tool_call("get_invoices", {"customer": f"c{i}"}, lambda customer: {"invoices": 2})
            v4 = prompt_version.endswith("v4")
            if v4 and rnd.random() < 0.6:
                tdn.tool_call("escalate", {"reason": "unsure"}, lambda reason: {"ticket": "T-1"})
            failed = rnd.random() < (0.35 if v4 else 0.08)
            tdn.llm_call(lambda p, **_: {"text": "Here is what happened with your bill.",
                                         "usage": {"input_tokens": 300, "output_tokens": 40}},
                         model="gpt-4o-mini", provider="openai", prompt=question, prompt_version=prompt_version)
        run.output("Here is what happened with your bill.")
        if failed:
            run.fail("customer reopened the ticket")
        return run.trace_id


ALL["account"] = account_agent
