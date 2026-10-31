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

There is one-line instrumentation for LangGraph, the OpenAI Agents SDK, CrewAI, AutoGen, Semantic Kernel and LlamaIndex (`agent_tracedown.integrations`). Runs can also be exported to any OpenTelemetry collector.

Documentation, benchmarks and the demo are in the [repository](https://github.com/nbn07/agent-tracedown). Licensed under Apache-2.0.
