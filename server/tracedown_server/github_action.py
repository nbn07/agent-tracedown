"""PR-comment report and regression gate for the GitHub Action.

The composite action in ``action.yml`` (repo root) starts a fresh server,
runs the caller's command so their agent's runs land in it, then calls this
module: it collects the runs, renders the Markdown table that gets posted as
a PR comment, compares each conversation's outcome with the baseline file
the workflow persists between runs, and exits non-zero when a conversation
that passed last time fails now (unless told not to).

Kept dependency-free (stdlib only) and separate from the server so the
formatting and the comparison are unit-testable without a running server.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

PASS = "success"
FAIL = "failure"


def fetch_runs(endpoint: str, project: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every run the (fresh) action server holds, newest first."""
    url = endpoint.rstrip("/") + "/api/v1/runs?limit=500"
    if project:
        url += "&project=" + urllib.parse.quote(project)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - caller's own server
        return list(json.loads(resp.read().decode("utf-8")).get("runs") or [])


def run_row(run: Dict[str, Any]) -> Dict[str, str]:
    """One table row for a run: conversation, result, root cause for failures."""
    cause = run.get("root_cause") or {}
    detail = ""
    if run.get("outcome") != PASS and cause.get("label"):
        detail = str(cause["label"])
        if cause.get("step") is not None:
            detail += f" (step {cause['step']})"
    return {"conversation": str(run.get("agent_name") or "(unnamed)"),
            "result": str(run.get("outcome") or "unknown"),
            "detail": detail}


def conversation_rows(runs: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """One row per conversation: it fails if any of its runs failed, and the
    root cause shown is its most recent failure's (runs arrive newest first)."""
    grouped: Dict[str, Dict[str, str]] = {}
    for run in runs:
        row = run_row(run)
        current = grouped.setdefault(row["conversation"],
                                     {"conversation": row["conversation"],
                                      "result": PASS, "detail": ""})
        if row["result"] == FAIL and current["result"] != FAIL:
            current.update(result=FAIL, detail=row["detail"])
    return [grouped[name] for name in sorted(grouped)]


def outcomes_by_conversation(rows: List[Dict[str, str]]) -> Dict[str, str]:
    """Latest word per conversation; any failure makes the conversation failed."""
    outcomes: Dict[str, str] = {}
    for row in rows:
        name = row["conversation"]
        if outcomes.get(name) != FAIL:
            outcomes[name] = row["result"]
    return outcomes


def find_regressions(baseline: Dict[str, str], current: Dict[str, str]) -> List[str]:
    """Conversations that passed in the baseline and fail now."""
    return sorted(name for name, outcome in current.items()
                  if outcome == FAIL and baseline.get(name) == PASS)


def format_comment(rows: List[Dict[str, str]], regressions: List[str],
                   new: List[str]) -> str:
    lines = ["## Tracedown report", ""]
    if regressions:
        lines += [f"**Regression:** {len(regressions)} conversation(s) that passed "
                  f"before now fail: {', '.join(regressions)}.", ""]
    if not rows:
        lines.append("No runs were recorded. Point your agent's exporter at the "
                     "server the action starts (http://127.0.0.1:7070) and make "
                     "sure the command actually runs it.")
        return "\n".join(lines) + "\n"
    lines += ["| Conversation | Result | Root cause |", "| --- | --- | --- |"]
    for row in rows:
        lines.append(f"| {row['conversation']} | {row['result']} | {row['detail'] or '—'} |")
    if new:
        lines += ["", f"First time seen (recorded in the baseline): {', '.join(new)}."]
    return "\n".join(lines) + "\n"


def load_baseline(path: Path) -> Dict[str, str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def save_baseline(path: Path, outcomes: Dict[str, str]) -> None:
    path.write_text(json.dumps(outcomes, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def post_comment(token: str, repo: str, pr_number: str, body: str) -> None:
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")
    req = urllib.request.Request(
        f"{api}/repos/{repo}/issues/{pr_number}/comments",
        data=json.dumps({"body": body}).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - fixed GitHub API host
        resp.read()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="tracedown-github-action",
                                 description="Build the Tracedown PR report and gate on regressions.")
    ap.add_argument("--endpoint", default="http://127.0.0.1:7070")
    ap.add_argument("--project", default=None)
    ap.add_argument("--baseline", default="tracedown-baseline.json")
    ap.add_argument("--fail-on-regression", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--comment", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    ap.add_argument("--pr", default="")
    ap.add_argument("--output", default=None, help="also write the report Markdown to this file")
    args = ap.parse_args(argv)

    try:
        runs = fetch_runs(args.endpoint, args.project)
    except (urllib.error.URLError, ValueError, OSError) as exc:
        print(f"tracedown action: could not read runs from {args.endpoint}: {exc}", file=sys.stderr)
        return 2

    rows = conversation_rows(runs)
    current = outcomes_by_conversation(rows)
    baseline_path = Path(args.baseline)
    baseline = load_baseline(baseline_path)
    regressions = find_regressions(baseline, current)
    new = sorted(name for name in current if name not in baseline)
    body = format_comment(rows, regressions, new)
    print(body)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(body)
    if args.output:
        Path(args.output).write_text(body, encoding="utf-8")

    merged = dict(baseline)
    merged.update(current)
    save_baseline(baseline_path, merged)

    if args.comment:
        token = os.environ.get("GITHUB_TOKEN", "")
        if token and args.repo and args.pr:
            try:
                post_comment(token, args.repo, args.pr, body)
            except (urllib.error.URLError, OSError) as exc:
                print(f"tracedown action: could not post the PR comment: {exc}", file=sys.stderr)
        else:
            print("tracedown action: no PR context (or no GITHUB_TOKEN); comment skipped.",
                  file=sys.stderr)

    if regressions and args.fail_on_regression:
        print(f"tracedown action: regression(s): {', '.join(regressions)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
