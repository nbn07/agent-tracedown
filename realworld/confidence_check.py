"""Does a higher score mean a more likely right first guess? (It does not; see LABELING.md.)

    python realworld/confidence_check.py        # Who&When (rules and the saved Haiku answers) and tau-bench test

Uses the saved judge answers only: no API calls. Needs the converted data in realworld/data/.
"""
import importlib.util
import json
import os
import sys

R = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(R, "server"), os.path.join(R, "sdk", "python"), os.path.join(R, "realworld", "whowhen")]
from tracedown_server.rca import analyze  # noqa: E402

import score as WW  # noqa: E402  (realworld/whowhen/score.py)

CUTS = (0.0, 0.5, 0.6, 0.7, 0.8, 0.9)


def ww_rows(subset, judged):
    WW.JUDGE["fn"] = None if not judged else (lambda p: (_ for _ in ()).throw(RuntimeError("answer missing from the cache")))
    WW.JUDGE["key"] = f"{subset}-nogt"
    rows = []
    for i, line in enumerate(open(os.path.join(R, "realworld", "data", f"whowhen-{subset}.jsonl"))):
        c = json.loads(line)
        c.setdefault("case_id", c.get("id", i))
        b = c["bundle"]
        by_id = {s["span_id"]: s for s in b["spans"]}
        rep = analyze(b["spans"], b["blobs"], judge=WW._judge_for(c) if judged else None)
        guess = None
        for f in rep["findings"]:
            s = by_id.get(f["span_id"])
            if s is None or "tdn.ww.index" not in s["attributes"]:
                continue
            idx, agent = s["attributes"]["tdn.ww.index"], s["attributes"]["tdn.ww.agent"]
            if agent in WW.TERMINALS:
                idx = WW._speaker_index_before(c, idx)
            guess = (f["score"], idx == c["mistake_step"])
            break
        rows.append(guess or (0.0, WW.last_step(c)[0][1] == c["mistake_step"]))
    return rows


def tau_rows():
    spec = importlib.util.spec_from_file_location("tau", os.path.join(R, "realworld", "score.py"))
    tau = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tau)
    rows = []
    for c in tau.load("test"):
        if c["label"].get("step") is None:
            continue
        f = (analyze(c["bundle"]["spans"], c["bundle"]["blobs"], judge=None)["findings"] or [None])[0]
        rows.append((f["score"], tau.located(f, c["label"])) if f else (0.0, False))
    return rows


def report(name, rows):
    print(f"\n{name}: {len(rows)} runs")
    for cut in CUTS:
        sel = [ok for s, ok in rows if s >= cut]
        if sel:
            print(f"  score >= {cut:.1f}: {len(sel):4d} runs ({100 * len(sel) / len(rows):5.1f}%)  first guess right {100 * sum(sel) / len(sel):5.1f}%")


if __name__ == "__main__":
    for path in (os.path.join(R, "realworld", "whowhen", "judge-answers-haiku-5-5.jsonl"),):
        for line in open(path):
            d = json.loads(line)
            WW.JUDGE["cache"][d["key"]] = d["answer"]
    for subset in ("algorithm", "hand"):
        report(f"Who&When {subset}, rules only", ww_rows(subset, False))
        report(f"Who&When {subset}, Haiku 5.5 judge", ww_rows(subset, True))
    report("tau-bench test, rules only", tau_rows())
