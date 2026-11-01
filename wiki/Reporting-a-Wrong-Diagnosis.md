# Reporting a Wrong Diagnosis

If Tracedown blamed the wrong step on one of your runs, that report is the single most useful thing you can send. Every report becomes a test case, and test cases are how the diagnosis gets better.

## How

1. Open a [**Wrong diagnosis** issue](https://github.com/nbn07/agent-tracedown/issues/new?template=wrong-diagnosis.yml).
2. Say what Tracedown blamed, and what really went wrong (which step, and why).
3. If you can, attach the run bundle, **redacted**:
   ```bash
   curl http://127.0.0.1:7070/api/v1/runs/<trace_id>/bundle > run.json
   ```
   Remove anything private (names, emails, keys, customer data) before attaching. Replacing values consistently (the same fake email everywhere) keeps the data flow intact, which matters for the diagnosis.
4. Can't share the run? Describe the steps in words. That still helps.

## What happens next

A maintainer reproduces it, turns it into a test case (`benchmark/` or `server/tests/`), and labels it `wrong-diagnosis`. Fixes are checked against the public benchmarks so one improvement doesn't break others.
