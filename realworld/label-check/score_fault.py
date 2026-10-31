"""Score the "who is at fault" verdict against a checked label set.

    python realworld/label-check/score_fault.py realworld/label-check/copilot-labels-50.csv

Truth, fixed before scoring (2026-10-30):
- verdict Wrong, kind "Customer caused it"                   -> user
- verdict Wrong, kind "Something else", note says no real mistake -> nobody (unfinished / no agent error)
- verdict Right, or Wrong with any other kind                -> agent
- Not sure                                                   -> skipped
"""
import csv
import json
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "..", "server")]
from tracedown_server.rca import analyze  # noqa: E402

NO_MISTAKE = re.compile(r"no real (agent )?(mistake|error)|made no real mistake", re.I)


def truth(r):
    if r["verdict"] == "Not sure" or not r["verdict"]:
        return None
    if r["verdict"] == "Wrong" and r["kind"] == "Customer caused it":
        return "user"
    if r["verdict"] == "Wrong" and r["kind"] == "Something else" and NO_MISTAKE.search(r["note"]):
        return "nobody"
    return "agent"


def main(path):
    rows = list(csv.DictReader(open(path)))
    cases = {}
    for line in open(os.path.join(HERE, "..", "data", "test.jsonl")):
        c = json.loads(line)
        cases[c["id"]] = c
    pairs = []
    for r in rows:
        t = truth(r)
        if t is None:
            continue
        b = cases[r["case_id"]]["bundle"]
        f = analyze(b["spans"], b["blobs"], judge=None)["fault"]
        pred = {"agent": "agent", "user": "user", "unfinished": "nobody", "unclear": "agent"}[f["verdict"]]
        pairs.append((r["case_id"], t, pred, f["reason"][:110]))
    n = len(pairs)
    print(f"{n} runs with a fault label: {dict(Counter(t for _, t, _, _ in pairs))}")
    acc = sum(t == p for _, t, p, _ in pairs)
    base = sum(t == "agent" for _, t, _, _ in pairs)
    print(f"accuracy {acc}/{n} = {acc / n:.0%}   (always 'agent': {base}/{n} = {base / n:.0%})")
    for cls in ("user", "nobody"):
        tp = sum(t == cls and p == cls for _, t, p, _ in pairs)
        fp = sum(t != cls and p == cls for _, t, p, _ in pairs)
        fn = sum(t == cls and p != cls for _, t, p, _ in pairs)
        prec = f"{tp}/{tp + fp}" if tp + fp else "n/a"
        print(f"  {cls:<7} found {tp}/{tp + fn} (recall), right when said {prec} (precision)")
    print("\nconfusion (truth -> predicted):", dict(Counter((t, p) for _, t, p, _ in pairs)))
    if "--show" in sys.argv:
        for cid, t, p, why in pairs:
            if t != p:
                print(f"  {cid:<22} truth={t:<7} said={p:<7} {why}")


if __name__ == "__main__":
    main(next((a for a in sys.argv[1:] if not a.startswith("--")), os.path.join(HERE, "copilot-labels-50.csv")))
