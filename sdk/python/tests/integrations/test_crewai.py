"""Emits real CrewAI event objects on CrewAI's own event bus (no LLM calls)."""
import json
import time

import pytest

ev = pytest.importorskip("crewai.events")

import agent_tracedown as tdn  # noqa: E402
from agent_tracedown.integrations import crewai as tdn_crew  # noqa: E402


def _emit(bus, event):
    fut = bus.emit(None, event)
    if fut is not None:
        fut.result(timeout=5)


def _wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end and not cond():
        time.sleep(0.02)


def test_crewai_events_become_one_run():
    mem = tdn.MemoryExporter()
    tdn.init(project="t", exporter=mem)
    listener = tdn_crew.instrument()
    bus = ev.crewai_event_bus
    C = lambda cls, **kw: cls.model_construct(**kw)  # noqa: E731 - skip validation of nested CrewAI objects
    _emit(bus, C(ev.CrewKickoffStartedEvent, type="crew_kickoff_started", crew_name="ResearchCrew",
                 inputs={"topic": "SDK version"}, event_id="e1"))
    _emit(bus, C(ev.AgentExecutionStartedEvent, type="agent_execution_started", agent_role="Researcher",
                 agent_id="a1", task_id="t1", task_name="research", task_prompt="find it", tools=[], event_id="e2",
                 parent_event_id="e1"))
    _emit(bus, C(ev.LLMCallStartedEvent, type="llm_call_started", call_id="c1", model="gpt-4o-mini",
                 messages=[{"role": "user", "content": "find it"}], temperature=0.1, agent_id="a1", task_id="t1",
                 event_id="e3", parent_event_id="e2"))
    _emit(bus, C(ev.LLMCallCompletedEvent, type="llm_call_completed", call_id="c1", model="gpt-4o-mini",
                 response="search", usage={"prompt_tokens": 20, "completion_tokens": 3}, agent_id="a1",
                 task_id="t1", event_id="e4", parent_event_id="e2"))
    for i in range(3):
        _emit(bus, C(ev.ToolUsageStartedEvent, type="tool_usage_started", tool_name="web_search",
                     tool_args={"q": "sdk version"}, run_attempts=1, agent_id="a1", task_id="t1",
                     event_id=f"s{i}", parent_event_id="e2"))
        _emit(bus, C(ev.ToolUsageErrorEvent, type="tool_usage_error", tool_name="web_search", agent_id="a1",
                     task_id="t1", error="rate limited", started_event_id=f"s{i}", event_id=f"f{i}",
                     parent_event_id="e2"))
    _emit(bus, C(ev.AgentExecutionCompletedEvent, type="agent_execution_completed", agent_role="Researcher",
                 agent_id="a1", task_id="t1", output="unknown", event_id="e5", parent_event_id="e1"))
    _emit(bus, C(ev.AgentExecutionStartedEvent, type="agent_execution_started", agent_role="Writer",
                 agent_id="a2", task_id="t2", task_name="write", task_prompt="write it", tools=[], event_id="e6",
                 parent_event_id="e1"))
    _emit(bus, C(ev.AgentExecutionCompletedEvent, type="agent_execution_completed", agent_role="Writer",
                 agent_id="a2", task_id="t2", output="I could not find it.", event_id="e7", parent_event_id="e1"))
    _emit(bus, C(ev.CrewKickoffCompletedEvent, type="crew_kickoff_completed", crew_name="ResearchCrew",
                 output="I could not find it.", total_tokens=23, event_id="e8", parent_event_id="e1"))
    _wait(lambda: listener.trace_ids and mem.trace(listener.trace_ids[0]))
    spans = mem.trace(listener.trace_ids[0])
    names = [s["name"] for s in sorted(spans, key=lambda s: s["attributes"]["tdn.step"])]
    assert names[:3] == ["invoke_agent ResearchCrew", "invoke_agent Researcher", "chat gpt-4o-mini"]
    assert names.count("execute_tool web_search") == 3 and "handoff Researcher -> Writer" in names
    tools = [s for s in spans if s["kind"] == "tool"]
    assert all(s["status"]["code"] == "ERROR" and s["attributes"]["tdn.agent.name"] == "Researcher" for s in tools)
    llm = next(s for s in spans if s["kind"] == "llm")
    assert llm["attributes"]["gen_ai.usage.input_tokens"] == 20
    root = next(s for s in spans if s["parent_span_id"] is None)
    assert json.loads(mem.blobs[root["output_digest"]]) == "I could not find it."
