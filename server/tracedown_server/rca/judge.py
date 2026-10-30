"""LLM judge: a semantic second opinion when the rules have no direct evidence.

Rules catch crashes, loops, stale sources and contradicted numbers. They cannot tell
that the agent exchanged the *wrong* valid item, or cancelled when the customer asked
to change the date. For a failed run whose best finding is weak, the judge reads a
compact transcript (request, each agent turn, each tool call and result, the outcome)
and names the step where the run went wrong and why.

Enable it with ``TDN_JUDGE=openai:<model>`` (``OPENAI_API_KEY``; ``OPENAI_BASE_URL`` for
compatible servers such as vLLM or Ollama) or ``TDN_JUDGE=anthropic:<model>``
(``ANTHROPIC_API_KEY``). Any ``callable(prompt) -> str`` works too.

The judge's answer is a finding like any other: ``detector="llm_judge"``, its own
explanation as evidence, and confidence capped at 0.9.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

JudgeFn = Callable[[str], str]

CATEGORIES = {
    "wrong_tool": "the agent called a tool/action it should not have (wrong action for the request or policy)",
    "bad_llm_output": "right action, wrong argument value (wrong item, order, date, amount, payment method)",
    "incomplete_task": "the agent stopped before doing everything the user asked",
    "policy_violation": "the action broke a stated rule (e.g. no confirmation, not allowed for this case)",
    "hallucination": "the final reply states something false or unsupported",
    "other": "none of the above",
}
WEAK = 0.8  # call the judge when the best rule finding is below this score


def _clip(v: Any, n: int = 300) -> str:
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s if len(s) <= n else s[: n - 1] + "…"


def transcript(run, max_steps: int = 120) -> str:
    """Compact, numbered view of the run. ``run`` is an ``rca.RunView``."""
    lines = [f"REQUEST: {_clip(run.input(run.root), 600)}"]
    for s in run.spans[:max_steps]:
        st = run.step(s)
        if s["kind"] == "llm":
            inp = run.input(s)
            if isinstance(inp, list):
                for m in inp:
                    if isinstance(m, dict) and m.get("role") == "user":
                        lines.append(f"[{st}] USER: {_clip(m.get('content'), 400)}")
            out = run.output(s)
            text = out.get("text") if isinstance(out, dict) else out
            if text:
                lines.append(f"[{st}] AGENT SAYS: {_clip(text, 400)}")
        elif s["kind"] == "tool":
            name = s["attributes"].get("gen_ai.tool.name", s["name"])
            res = s["status"]["message"] if s["status"]["code"] == "ERROR" else _clip(run.output(s), 250)
            lines.append(f"[{st}] TOOL {name}({_clip(run.input(s), 300)}) -> {res}")
        elif s["kind"] in ("retrieval", "memory", "decision"):
            lines.append(f"[{st}] {s['kind'].upper()} {s['name']}: {_clip(run.output(s), 200)}")
    lines.append(f"OUTCOME: {run.root['status']['message'] or run.root['attributes'].get('tdn.run.outcome')}")
    lines.append(f"FINAL REPLY: {_clip(run.output(run.root), 500)}")
    return "\n".join(lines)


def build_prompt(run, candidates: List[Dict[str, Any]]) -> str:
    cats = "\n".join(f"- {k}: {v}" for k, v in CATEGORIES.items())
    cand = "\n".join(f"- step {c['step']}: {c['label']} ({c['title'][:120]})" for c in candidates[:5]) or "- none"
    return (
        "You are debugging a failed AI agent run. Find the EARLIEST step where the agent went wrong "
        "(the root cause, not where the failure became visible).\n\n"
        f"Categories:\n{cats}\n\nAutomatic checks flagged (may be wrong):\n{cand}\n\n"
        f"Run transcript:\n{transcript(run)}\n\n"
        'Answer with JSON only: {"step": <int>, "category": "<category>", "confidence": <0..1>, '
        '"explanation": "<one sentence>"}'
    )


def parse(answer: str) -> Optional[Dict[str, Any]]:
    m = re.search(r"\{.*\}", answer or "", re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(d.get("step"), int) or d.get("category") not in CATEGORIES:
        return None
    d["confidence"] = max(0.0, min(float(d.get("confidence", 0.5)), 1.0))
    return d


# ----------------------------------------------------------------------------- providers (stdlib HTTP)

def _post(url: str, headers: Dict[str, str], body: Dict[str, Any], timeout: float = 60) -> Dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:  # keep the provider's explanation
        raise RuntimeError(f"{url}: HTTP {e.code}: {e.read()[:500].decode(errors='replace')}") from None


def openai_judge(model: str, api_key: Optional[str] = None, base_url: Optional[str] = None) -> JudgeFn:
    key = api_key or os.environ.get("OPENAI_API_KEY", "")
    base = (base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")

    def call(prompt: str) -> str:
        r = _post(f"{base}/chat/completions", {"Authorization": f"Bearer {key}"},
                  {"model": model, "temperature": 0, "messages": [{"role": "user", "content": prompt}]})
        return r["choices"][0]["message"]["content"]
    return call


def anthropic_judge(model: str, api_key: Optional[str] = None) -> JudgeFn:
    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    # Newer models think before answering by default, and thinking counts against
    # max_tokens: a small budget returns no text at all. Leave room for both.
    body = {"model": model, "max_tokens": int(os.environ.get("TDN_JUDGE_MAX_TOKENS", "4000")), "temperature": 0}

    def call(prompt: str) -> str:
        req = dict(body, messages=[{"role": "user", "content": prompt}])
        try:
            r = _post("https://api.anthropic.com/v1/messages", {"x-api-key": key, "anthropic-version": "2023-06-01"}, req)
        except RuntimeError as e:
            if "temperature" not in str(e) or "temperature" not in req:
                raise
            body.pop("temperature", None)  # newer models fix their own sampling settings
            return call(prompt)
        return "".join(b.get("text", "") for b in r.get("content", []))
    return call


def from_spec(spec: Optional[str]) -> Optional[JudgeFn]:
    """``openai:gpt-4.1-mini`` / ``anthropic:claude-sonnet-4-5`` / None."""
    if not spec:
        return None
    provider, _, model = spec.partition(":")
    if provider == "openai":
        return openai_judge(model)
    if provider == "anthropic":
        return anthropic_judge(model)
    raise ValueError(f"unknown judge provider {provider!r}; use openai:<model> or anthropic:<model>")


_default: Dict[str, Optional[JudgeFn]] = {}


def default_judge() -> Optional[JudgeFn]:
    if "fn" not in _default:
        _default["fn"] = from_spec(os.environ.get("TDN_JUDGE"))
    return _default["fn"]
