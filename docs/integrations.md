# Integrations

Add one line to your agent, and every run is recorded and diagnosed. The SDKs have no dependencies.

```bash
pip install agent-tracedown          # Python
npm install agent-tracedown          # TypeScript
```

```python
import agent_tracedown as tdn
tdn.init(project="support-bot", endpoint="http://127.0.0.1:7070")
```

## Frameworks

| Framework | One line |
|---|---|
| LangChain, LangGraph | `graph.invoke(x, config={"callbacks": [tdn_lc.callback("MyGraph")]})` |
| OpenAI Agents SDK | `tdn_oa.instrument()` |
| Claude Agent SDK | `async for m in tdn_claude.trace_stream(query(prompt=p, options=o), name="Agent")` |
| Google ADK | `App(name="support", root_agent=agent, plugins=[tdn_adk.TracedownPlugin()])` |
| Pydantic AI | `result = await tdn_pai.run(agent, prompt)` |
| MCP clients | `tdn_mcp.instrument(client)` |
| CrewAI | `tdn_crew.instrument()` |
| AutoGen AgentChat | `async for m in tdn_ag.trace_stream(team.run_stream(task=t), name="Team")` |
| Semantic Kernel | `tdn_sk.instrument(kernel)` |
| LlamaIndex | `tdn_li.instrument()` |
| OpenAI and Anthropic clients | `tdn.plugins.instrument("openai", client)` |
| Vercel AI SDK (TypeScript) | `generateText({ model: traceModel(model), tools: traceTools(tools) })` |
| OpenAI Agents SDK (TypeScript) | `await instrument()` from `agent-tracedown/openai-agents` |

Python adapters live in `agent_tracedown.integrations`. Each one is tested in CI against the real library.

## Without a framework

```python
with tdn.run("CalendarAssistant", input=message) as run:
    reply = tdn.llm_call(call_model, model="gpt-4o-mini", prompt=messages)
    tdn.tool_call("create_event", args, create_event, schema=SCHEMA)
    run.output(reply)
```

The SDK READMEs cover every call, redaction and replay: [Python](https://github.com/nbn07/agent-tracedown/blob/main/sdk/python/README.md) · [TypeScript](https://github.com/nbn07/agent-tracedown/blob/main/sdk/typescript/README.md).

## Exports

Tracedown also sends spans on as OTLP (Jaeger, Tempo, Datadog or any collector) and serves Prometheus metrics at `/metrics`. See [Configuration](CONFIGURATION.md).
