# Adding a Demo Story

The demo agents are short, scripted agents that fail in realistic ways. They need no API keys (the "models" are fixed functions), so every run reproduces exactly. They power `tracedown up`, the public demo, the tests and the TypeScript parity check.

Current stories: calendar (wrong date format), refund (outdated policy), loop (repeated search), pricing (made-up number), account (wrong tool chosen), checkout (says the order is confirmed when the payment failed), injection (a document that gives the model orders), order_status (the customer's details never match an account: not an agent bug).

## Steps

1. **Write the agent** in [`server/tracedown_server/demo/agents.py`](https://github.com/nbn07/agent-tracedown/blob/main/server/tracedown_server/demo/agents.py): a function that uses `tdn.run`, `tdn.llm_call`, `tdn.tool_call` etc., with a fake model that returns the wrong thing. Keep it under about 40 lines and make the mistake obvious to a person reading the run.
2. **Register it** in the `ALL` dictionary at the bottom of its section: `ALL["mystory"] = my_agent`.
3. **Add a test** in `server/tests/test_demo_stories.py` asserting the root cause's category and step, and that the evidence mentions the key value.
4. **Check the TypeScript parity test** still passes (`npm test` in `sdk/typescript`). It runs the Python agents whose names exist in the TypeScript demo; a Python-only story is fine.
5. **Look at it in the app:** `tracedown up --db /tmp/demo.db` and open the run. Does the verdict read well to someone new?

Good stories show one clear mistake, a symptom in a *different* step from the cause, and evidence that teaches something.
