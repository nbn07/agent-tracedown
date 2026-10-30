"""Run τ-bench live with your own model, then turn its failures into Tracedown cases.

    export OPENAI_API_KEY=...            # or ANTHROPIC_API_KEY
    python realworld/live/tau_bench_live.py --tau-bench ../tau-bench --env retail \\
        --model gpt-4.1-mini --provider openai --tasks 0-19 --trials 1
    python realworld/score.py --file realworld/data/live-tau-retail-gpt-4.1-mini.jsonl

τ-bench runs the conversation (agent model + simulated customer, both through LiteLLM)
and writes its usual results file; this script converts that file with the same rules as
the historical data, so live and historical numbers are comparable. Cost: roughly 10–30
model calls per task for the agent plus the same for the simulated customer.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import subprocess
import sys

from common import ROOT, write_cases

sys.path.insert(0, os.path.join(ROOT, "realworld"))
from tau_bench_import import convert_records  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tau-bench", required=True, help="clone of sierra-research/tau-bench (pip install -e it)")
    ap.add_argument("--env", choices=["airline", "retail"], default="retail")
    ap.add_argument("--model", required=True)
    ap.add_argument("--provider", default="openai", help="LiteLLM provider: openai, anthropic, ...")
    ap.add_argument("--user-model", default=None, help="simulated customer model (default: same as --model)")
    ap.add_argument("--tasks", default="0-9", help="task index range, e.g. 0-19")
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--concurrency", type=int, default=4, help="tasks run in parallel")
    ap.add_argument("--dry-run", action="store_true", help="print the τ-bench command and exit")
    ap.add_argument("--results", help="skip running; convert an existing τ-bench results JSON")
    a = ap.parse_args()

    start, end = (int(x) for x in a.tasks.split("-"))
    out_dir = os.path.join(ROOT, "realworld", "data", "tau-live")
    if a.results:
        path = a.results
    else:
        cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tau_run.py"), "--agent-strategy", "tool-calling", "--env", a.env,
               "--model", a.model, "--model-provider", a.provider,
               "--user-model", a.user_model or a.model, "--user-model-provider", a.provider,
               "--user-strategy", "llm", "--start-index", str(start), "--end-index", str(end + 1),
               "--num-trials", str(a.trials), "--max-concurrency", str(a.concurrency), "--log-dir", out_dir]
        print("running:", " ".join(cmd), f"(in {a.tau_bench})")
        if a.dry_run:
            return
        subprocess.run(cmd, cwd=a.tau_bench, check=True)
        path = max(glob.glob(os.path.join(out_dir, "*.json")), key=os.path.getmtime)
    records = json.load(open(path))
    # runs that died on an API error never reached the customer: they are not agent failures
    broken = [r for r in records if (r.get("info") or {}).get("error")]
    if broken:
        print(f"skipping {len(broken)} runs that ended in an API error, e.g. {broken[0]['info']['error'][:120]}")
    records = [r for r in records if not (r.get("info") or {}).get("error")]
    name = a.model.replace("/", "_")
    cases = convert_records(records, a.env, a.model, f"live-{name}")
    for c in cases:
        c["success"] = False
    ok = sum(1 for r in records if r.get("reward", 0) >= 1)
    print(f"τ-bench: {ok}/{len(records)} runs succeeded")
    write_cases(os.path.join(ROOT, "realworld", "data", f"live-tau-{a.env}-{name}.jsonl"), cases)


if __name__ == "__main__":
    main()
