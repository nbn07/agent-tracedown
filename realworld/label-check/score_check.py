"""Score the τ-bench reference labels, and Tracedown, against a checked label set.

    python realworld/label-check/score_check.py realworld/label-check/copilot-labels-50.csv

A checked step comes from a "Right" verdict (the reference step) or a "Wrong" verdict with
a step. A prediction counts if it names that step, or names a tool call that the model turn
at that step issued.
"""
import csv
import json
import os
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "..", "server"), os.path.join(HERE, "..")]
from tracedown_server.rca import analyze  # noqa: E402
from score import first_write  # noqa: E402


def hit(pred, truth, spans):
    if pred is None or truth is None:
        return False
    pred = int(pred)
    if pred == truth:
        return True
    sp = next((s for s in spans if s["attributes"]["tdn.step"] == pred), None)
    return bool(sp) and sp["attributes"].get("tdn.tau.emitted_by_step") == truth


def main(path):
    rows = list(csv.DictReader(open(path)))
    cases = {}
    for line in open(os.path.join(HERE, "..", "data", "test.jsonl")):
        c = json.loads(line)
        cases[c["id"]] = c
    judge_file = os.path.join(HERE, "..", "judge-answers-haiku-5-5.jsonl")
    jc = {json.loads(x)["key"]: json.loads(x)["answer"] for x in open(judge_file)} if os.path.exists(judge_file) else {}
    v = Counter(r["verdict"] for r in rows)
    decided = v["Right"] + v["Wrong"]
    print(f"{len(rows)} checked: right {v['Right']}, wrong {v['Wrong']}, not sure {v['Not sure']}; "
          f"reference label agreement {v['Right'] / decided:.0%} of decided")
    print("kinds when wrong:", dict(Counter(r["kind"] for r in rows if r["verdict"] == "Wrong")))
    res, n = Counter(), 0
    for r in rows:
        c = cases[r["case_id"]]
        spans, blobs, lab = c["bundle"]["spans"], c["bundle"]["blobs"], c["label"]
        if r["verdict"] == "Right" and lab["step"] is not None:
            truth = lab["step"]
        elif r["verdict"] == "Wrong" and r["checked_step"]:
            truth = int(r["checked_step"])
        else:
            continue
        n += 1
        rules = analyze(spans, blobs, judge=None)["findings"][:3]
        ans = jc.get(r["case_id"])
        judged = analyze(spans, blobs, judge=(lambda p, a=ans: a) if ans else None)["findings"][:3]
        fw = first_write(c["bundle"])
        res["Reference label"] += hit(lab["step"], truth, spans)
        res["Tracedown rules, top 1"] += hit(rules[0]["step"] if rules else None, truth, spans)
        res["Tracedown rules, top 3"] += any(hit(f["step"], truth, spans) for f in rules)
        res["Tracedown + judge, top 1"] += hit(judged[0]["step"] if judged else None, truth, spans)
        res["Tracedown + judge, top 3"] += any(hit(f["step"], truth, spans) for f in judged)
        res["Baseline: first write call"] += hit(fw[0]["step"] if fw else None, truth, spans)
    print(f"\n{n} runs with a checked step")
    for k, x in res.items():
        print(f"  {k:<28} {x}/{n} = {x / n:.0%}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "copilot-labels-50.csv"))
