"""Make a human labelling sheet: a random sample of real failures, each with a readable
transcript, the mechanical reference label and Tracedown's top three.

    python realworld/label_sheet.py --split test --n 50          # writes realworld/labeling/
    # fill in the four "human_*" columns, then:
    python realworld/label_sheet.py --score realworld/labeling/sheet.csv

The scorer reports (1) how often the mechanical label was right, which bounds how much the
τ-bench numbers can be trusted, and (2) Tracedown's accuracy against the human labels.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", "server"), os.path.join(HERE, "..", "sdk", "python")]

COLUMNS = ["id", "transcript_file", "reference_category", "reference_step", "tracedown_top3",
           "human_reference_correct", "human_step", "human_category", "human_notes"]
HELP = ("human_reference_correct: y / n / unsure · human_step: the step number where the agent first went wrong · "
        "human_category: called_wrong_tool / used_wrong_tool_argument / goal_partially_completed / missing_output / "
        "user_caused / other")


def make(split: str, n: int, seed: int) -> None:
    from tracedown_server.rca import RunView, analyze
    from tracedown_server.rca.judge import transcript
    cases = [json.loads(line) for line in open(os.path.join(HERE, "data", f"{split}.jsonl"))]
    random.Random(seed).shuffle(cases)
    out_dir = os.path.join(HERE, "labeling")
    os.makedirs(os.path.join(out_dir, "transcripts"), exist_ok=True)
    rows = []
    for c in cases[:n]:
        b = c["bundle"]
        rep = analyze(b["spans"], b["blobs"], judge=None)
        top3 = "; ".join(f"{f['category']}@{f['step']}" for f in rep["findings"][:3])
        fname = f"transcripts/{c['id']}.md"
        with open(os.path.join(out_dir, fname), "w") as f:
            f.write(f"# {c['id']}\n\n**Customer goal (τ-bench instruction):** {c.get('instruction')}\n\n")
            f.write("**Expected database changes:**\n" + "\n".join(f"- `{e}`" for e in c["label"]["expected"]) + "\n\n")
            f.write(f"**Reference label:** {c['label']['category']} at step {c['label']['step']} "
                    f"({c['label']['detail']})\n\n**Tracedown top 3:** {top3}\n\n")
            f.write("```\n" + transcript(RunView(b["spans"], b["blobs"]), max_steps=400) + "\n```\n")
        rows.append({"id": c["id"], "transcript_file": fname, "reference_category": c["label"]["category"],
                     "reference_step": c["label"]["step"], "tracedown_top3": top3,
                     "human_reference_correct": "", "human_step": "", "human_category": "", "human_notes": ""})
    with open(os.path.join(out_dir, "sheet.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} cases to {out_dir}/sheet.csv and {out_dir}/transcripts/\n{HELP}")


def score(path: str) -> None:
    rows = [r for r in csv.DictReader(open(path)) if r["human_step"] or r["human_reference_correct"]]
    if not rows:
        print("no labelled rows yet")
        return
    judged = [r for r in rows if r["human_reference_correct"] in ("y", "n")]
    ok = sum(r["human_reference_correct"] == "y" for r in judged)
    print(f"labelled rows: {len(rows)}")
    if judged:
        print(f"mechanical reference label correct: {ok}/{len(judged)} = {ok / len(judged):.0%}")
    stepped = [r for r in rows if r["human_step"].strip().isdigit()]
    if stepped:
        def hit(r, k):
            preds = [p.split("@")[1] for p in r["tracedown_top3"].split("; ")[:k] if "@" in p]
            return r["human_step"].strip() in preds
        h1 = sum(hit(r, 1) for r in stepped)
        h3 = sum(hit(r, 3) for r in stepped)
        print(f"Tracedown vs human step: top-1 {h1}/{len(stepped)} = {h1 / len(stepped):.0%}, "
              f"top-3 {h3}/{len(stepped)} = {h3 / len(stepped):.0%}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", default="test")
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--score")
    a = ap.parse_args()
    score(a.score) if a.score else make(a.split, a.n, a.seed)


if __name__ == "__main__":
    main()
