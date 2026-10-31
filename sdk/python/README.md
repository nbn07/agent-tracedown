# agent-tracedown

The Python SDK for [Tracedown](https://github.com/nbn07/agent-tracedown), the flight recorder for AI agents. When an agent fails, Tracedown finds the step that caused it, not just the step that crashed. Then you can replay the run with your fix.

```bash
pip install agent-tracedown                 # the SDK: no dependencies
pip install agent-tracedown-server          # root-cause analysis, importers, server and UI
```

```python
import agent_tracedown as tdn
tdn.init(project="support-bot", endpoint="http://localhost:7070")

with tdn.run("CalendarAssistant", input=user_msg) as run:
    out = tdn.llm_call(call_model, model="gpt-4o-mini", prompt=messages)
    result = tdn.tool_call("calendar_create_event", args, create_event)
    run.output(answer)
```

There is one-line instrumentation for LangGraph, the OpenAI Agents SDK, the Claude Agent SDK, Google ADK, Pydantic AI, MCP, CrewAI, AutoGen, Semantic Kernel and LlamaIndex (`agent_tracedown.integrations`). Runs can also be exported to any OpenTelemetry collector.

## One-line run tracing

For a single-function agent, use `@tdn.traced_run(name)` instead of opening the
run context yourself. The decorator records the first argument as input, the
return value as output, and re-raises exceptions after marking the run failed.
It supports both synchronous and asynchronous functions.

```python
@tdn.traced_run("CalendarAssistant")
def answer(question):
    return call_model(question)
```

## Framework integrations

The newer ones, briefly. Each records agents, model calls (with token usage and cost where the framework reports it) and tool calls, and marks the step where a failure happened.

**Claude Agent SDK** (`pip install "agent-tracedown[claude-agent-sdk]"`). Wrap the message stream; messages pass through unchanged. Subagents started with the Task tool nest under their own agent span, and the run's cost comes from `ResultMessage.total_cost_usd`.

```python
from agent_tracedown.integrations import claude_agent as tdn_claude

async for msg in tdn_claude.trace_stream(query(prompt=p, options=opts), name="CodingAgent", prompt=p):
    ...
```

**Google ADK** (`pip install "agent-tracedown[google-adk]"`). Add the plugin to your app; it only observes.

```python
from agent_tracedown.integrations import google_adk as tdn_adk

app = App(name="support", root_agent=root_agent, plugins=[tdn_adk.TracedownPlugin()])
runner = InMemoryRunner(app=app)
```

**Pydantic AI** (`pip install "agent-tracedown[pydantic-ai]"`). Run the agent through the wrapper; runs that raise are recorded too.

```python
from agent_tracedown.integrations import pydantic_ai as tdn_pai

result = await tdn_pai.run(agent, "Refund order 42")      # or tdn_pai.run_sync(...)
```

**MCP** (`pip install "agent-tracedown[mcp]"`). Instrument a `mcp.Client` or `ClientSession`; inside a run, every `call_tool` becomes a step and `list_tools` records the server's tools and their input schemas.

```python
from agent_tracedown.integrations import mcp as tdn_mcp

tdn_mcp.instrument(client, server="billing")   # or tdn_mcp.instrument_all()
with tdn.run("SupportAgent", input=question):
    await client.list_tools()
    await client.call_tool("refund", {"order_id": 42})
```

Documentation, benchmarks and the demo are in the [repository](https://github.com/nbn07/agent-tracedown). Licensed under Apache-2.0.
