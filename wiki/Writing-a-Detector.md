# Writing a Detector

A detector is one small rule that looks at a run and reports a kind of problem. It's like adding one more test to the mechanic's checklist.

## The contract

A detector is a function in [`server/tracedown_server/rca/detectors.py`](https://github.com/nbn07/agent-tracedown/blob/main/server/tracedown_server/rca/detectors.py), decorated with `@detector`. It receives a `RunView` and returns a list of `Finding`s.

`RunView` gives you:

| | |
|---|---|
| `run.spans` | All spans, ordered by step |
| `run.root` | The run's root span |
| `run.of_kind("tool")` | Spans of one kind: `llm`, `tool`, `retrieval`, `memory`, `agent`… |
| `run.input(span)`, `run.output(span)` | Decoded payloads (JSON if possible, else text, else `None`) |
| `run.step(span)` | The span's step number |
| `run.by_id[span_id]` | Look up a span |

## A worked example

The existing `loops` detector, slightly shortened: the same tool called three or more times with identical arguments.

```python
@detector
def loops(run: RunView) -> List[Finding]:
    out = []
    groups = defaultdict(list)
    for s in run.of_kind("tool"):
        groups[(s["attributes"].get("gen_ai.tool.name", s["name"]), s.get("input_digest"))].append(s)
    for (tool, _), spans in groups.items():
        if len(spans) >= 3:
            steps = [run.step(s) for s in spans]
            out.append(Finding(
                category="loop", title=f"{tool}() called {len(spans)} times with identical arguments",
                span_id=spans[0]["span_id"], step=steps[0], confidence=0.9, detector="loops",
                evidence=[f"steps {steps} share one argument digest"],
            ))
    return out
```

## Checklist

1. **Category.** Reuse one from `CATEGORY_LABELS` if it fits; otherwise add one there with a short human label. A new category also needs a fix text in `server/tracedown_server/incidents/__init__.py` and, if the TypeScript SDK has a matching category list, the same key there.
2. **Evidence.** At least one sentence a person can check against the run ("step 7 returned 0 documents"). No unexplained numbers.
3. **Symptom or suspect?** If your finding marks where a failure *showed up* (an error), set `symptom=True`; the walk-back will look for its cause. If it marks something *wrong in itself*, leave it `False`.
4. **Confidence.** It only orders findings. Look at the existing values: direct evidence is around 0.85–0.9; a strong prior like "first state change" is lower (about 0.55). Don't outrank direct evidence with a guess.
5. **No false alarms on healthy runs.** `test_successful_grounded_run_has_no_findings` must still pass. Think about benign text that might match your pattern (for example "Our assistant: you must sign in first" is not a prompt injection).
6. **A test** in `server/tests/`: build a tiny run with the SDK and a `MemoryExporter`, call `analyze()`, and assert the root cause's category and step. Add a negative test too.
7. **A demo story** if it makes a good one: see [Adding a Demo Story](Adding-a-Demo-Story).
8. **Benchmarks.** Run Who&When and τ-bench before and after, and say in the pull request whether the numbers moved ([Accuracy and Benchmarks](Accuracy-and-Benchmarks)).
9. **Docs.** Add a row to the detector table in `docs/HOW-IT-DECIDES.md`.

## Test skeleton

```python
import agent_tracedown as tdn
from tracedown_server.rca import analyze

def test_my_detector_flags_the_problem():
    mem = tdn.MemoryExporter()
    tdn.init(project="t", exporter=mem)
    with tdn.run("Bot", input="What is our refund window?") as run:
        tdn.retrieval("refund window", lambda q: [])
        run.fail("user said the answer was wrong")
    report = analyze(mem.trace(run.trace_id), mem.blobs)
    assert report["root_cause"]["category"] == "retrieval_failure"
```
