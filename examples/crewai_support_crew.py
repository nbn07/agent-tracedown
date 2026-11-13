"""A two-agent CrewAI crew with a deliberate bug, diagnosed by Tracedown.

    pip install crewai agent-tracedown-server
    tracedown up                                            # terminal 1: server + web app
    python examples/crewai_support_crew.py --endpoint http://localhost:7070
    python examples/crewai_support_crew.py                  # offline: diagnosis printed locally

The crew answers a refund question. A researcher agent looks up the current policy
($80) but its summary waves at "the full amount from the old policy" instead of the
number it just read, and the writer agent fills in the old plan's $120. Nothing
crashes — the crew reports success — but the customer is promised money the policy
does not pay. Tracedown's claims check catches it: the reply states $120, while the
only policy the crew actually read says $80.

No API key is needed: the agents run on ScriptedLLM, a stand-in model that returns
canned replies, so every run reproduces exactly. Swap it for a real CrewAI LLM and
the same instrumentation records the real thing.
"""
import argparse
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(_ROOT, "sdk", "python"), os.path.join(_ROOT, "server")]

import agent_tracedown as tdn  # noqa: E402
from agent_tracedown.integrations import crewai as tdn_crew  # noqa: E402
from crewai import Agent, Crew, Process, Task  # noqa: E402
from crewai.events.types.llm_events import LLMCallType  # noqa: E402
from crewai.llms.base_llm import BaseLLM, llm_call_context  # noqa: E402
from crewai.tools import tool  # noqa: E402


class ScriptedLLM(BaseLLM):
    """A stand-in model: returns the given replies in order (the last one repeats).

    Emits CrewAI's LLM call events like a real provider does, so the run records
    every model call. `supports_function_calling` is off, so agents use the
    text protocol: a reply either names an Action or gives the Final Answer.
    """

    def __init__(self, replies):
        super().__init__(model="scripted-fake")
        self._replies = list(replies)

    def supports_function_calling(self):
        return False

    def call(self, messages, tools=None, callbacks=None, available_functions=None,
             from_task=None, from_agent=None, response_model=None, **_):
        with llm_call_context():
            self._emit_call_started_event(messages, tools=tools, callbacks=callbacks,
                                          available_functions=available_functions,
                                          from_task=from_task, from_agent=from_agent)
            reply = self._replies.pop(0) if len(self._replies) > 1 else self._replies[0]
            self._emit_call_completed_event(reply, LLMCallType.LLM_CALL,
                                            from_task=from_task, from_agent=from_agent)
            return reply


@tool("refund_policy")
def refund_policy(query: str) -> str:
    """Look up the current refund policy."""
    return ("Current policy (refund-policy.md, version 2026.2): annual plan cancellations "
            "within 30 days receive a prorated refund of $80. Refunds are issued to the "
            "original payment method within 5 business days.")


def build_crew() -> Crew:
    researcher = Agent(
        role="Policy researcher", goal="Find out how much the customer is owed",
        backstory="You check the policy documents before answering.", tools=[refund_policy],
        llm=ScriptedLLM([
            'Thought: I need the current refund policy.\n'
            'Action: refund_policy\nAction Input: {"query": "annual plan cancellation refund"}',
            # the bug: the summary drops the $80 it just read and points at the old policy
            "Thought: I have what I need.\n"
            "Final Answer: The customer cancelled an annual plan after 10 days. "
            "They are owed the full amount from the old policy.",
        ]), verbose=False)
    writer = Agent(
        role="Support writer", goal="Reply to the customer with the refund amount",
        backstory="You write short, friendly replies using the researcher's findings.",
        llm=ScriptedLLM([
            # so the writer fills the gap with the old plan's $120
            "Thought: The researcher confirmed the amount, so I can write the reply.\n"
            "Final Answer: Good news: your refund of $120 will arrive within 5 business days.",
        ]), verbose=False)
    find_amount = Task(
        description="The customer cancelled their annual plan after 10 days. "
                    "Find out how much they will get back.",
        expected_output="The refund amount owed to the customer.", agent=researcher)
    send_reply = Task(
        description="Reply to the customer, telling them how much they will get back and when.",
        expected_output="A short reply to the customer confirming the refund amount.", agent=writer)
    return Crew(agents=[researcher, writer], tasks=[find_amount, send_reply],
                process=Process.sequential, verbose=False)


def _stack_trace_from_server(endpoint: str, trace_id: str, wait: float = 60.0) -> str:
    import json
    import urllib.error
    import urllib.request
    deadline = time.time() + wait
    while True:
        try:
            with urllib.request.urlopen(f"{endpoint}/api/v1/runs/{trace_id}/stacktrace", timeout=10) as r:
                return r.read().decode()
        except urllib.error.HTTPError as e:
            if e.code not in (404, 409) or time.time() > deadline:
                raise
            time.sleep(0.5)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--endpoint", help="a running Tracedown server (tracedown up); "
                                       "default: diagnose locally, no server needed")
    args = ap.parse_args()

    mem = None
    if args.endpoint:
        tdn.init(project="crewai-tutorial", endpoint=args.endpoint)
    else:
        mem = tdn.MemoryExporter()
        tdn.init(project="crewai-tutorial", exporter=mem)
    listener = tdn_crew.instrument()
    result = build_crew().kickoff()
    print(f"crew reply: {result.raw}")
    trace_id = listener.trace_ids[-1]

    if args.endpoint:
        print(f"\nrun {trace_id} — open it in the web app, or read the diagnosis here:\n")
        print(_stack_trace_from_server(args.endpoint, trace_id))
    else:
        for _ in range(250):  # the listener exports from CrewAI's worker threads
            if mem.trace(trace_id):
                break
            time.sleep(0.02)
        from tracedown_server.rca import analyze
        print(f"\nrun {trace_id} — diagnosis:\n")
        print(analyze(mem.trace(trace_id), mem.blobs)["stack_trace"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
