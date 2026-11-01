# Adding a Framework Integration

An integration lets people trace an agent framework with one line instead of wrapping every call by hand. Think of it as a plug adapter: the framework has its own socket shape, and the adapter turns its events into Tracedown spans.

## Where

- Python: [`sdk/python/agent_tracedown/integrations/`](https://github.com/nbn07/agent-tracedown/tree/main/sdk/python/agent_tracedown/integrations), one file per framework.
- TypeScript: [`sdk/typescript/src/integrations/`](https://github.com/nbn07/agent-tracedown/tree/main/sdk/typescript/src/integrations).

Read an existing one first. `pydantic_ai.py` is a good, compact example; `langchain.py` shows a callback handler.

## Two patterns

| The framework gives you… | Use |
|---|---|
| A place to wrap calls in your own code (you call the model and tools) | `tdn.run`, `tdn.llm_call`, `tdn.tool_call` context managers. Replay works for free. |
| Callbacks or events, possibly on other threads | `ManualTrace`: `start(key, name, kind, parent=…)`, `end(key, output=…, error=…)`, `finish(output=…)` |

```python
from ..tracer import ManualTrace

t = ManualTrace("ResearchCrew", input=inputs)
t.start("agent-1", "Researcher", "agent")
t.start("tool-7", "execute_tool web_search", "tool", parent="agent-1", input=args,
        attrs={"gen_ai.tool.name": "web_search"})
t.end("tool-7", output=result)
t.finish(output=answer)
```

## Rules

1. **Import the framework lazily** (inside functions), so `import agent_tracedown` never needs it.
2. **Never raise from tracing code.** If the framework's event shape surprises you, record what you can and carry on.
3. **Record what the detectors need:** the tool name (`gen_ai.tool.name`), tool arguments and errors, model name and token usage, retrieved documents as a list of `{id, text, source, score}`, and the tools offered to the model (`declare_tools`).
4. **Failed runs matter most.** Make sure an exception inside the framework still produces a finished run with the error recorded.
5. **Add an extra** in `sdk/python/pyproject.toml` pinning the version you tested, e.g. `myframework = ["myframework>=1.2"]`.
6. **Add a test** under `sdk/python/tests/integrations/` that drives the real library **without network** (a fake model client), and add the framework to the `frameworks` matrix in `.github/workflows/ci.yml`.
7. **Document it**: a row in the integrations table in the README and `docs/integrations.md`, with the one-line usage.

## Done when

- A failing demo-style run through the framework gives the same root cause as the hand-written equivalent.
- `PYTHONPATH=sdk/python pytest sdk/python/tests/integrations` passes with the extra installed.
