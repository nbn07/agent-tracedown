"""Shared plumbing for the live harnesses: case files and an optional Tracedown server."""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path[:0] = [os.path.join(ROOT, "sdk", "python"), os.path.join(ROOT, "server"), os.path.join(ROOT, "realworld")]

import agent_tracedown as tdn  # noqa: E402


def exporter(endpoint: Optional[str], api_key: Optional[str] = None, project: str = "realworld") -> tdn.MemoryExporter:
    """Keep every run in memory (to write case files) and, if given, send it to a server too."""
    mem = tdn.MemoryExporter()
    if endpoint:
        tdn.init(project=project, exporter=tdn.FanoutExporter(mem, tdn.HttpExporter(endpoint, api_key, project)))
    else:
        tdn.init(project=project, exporter=mem)
    return mem


def write_cases(path: str, cases: List[Dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for c in cases:
            f.write(json.dumps(c) + "\n")
    failed = sum(1 for c in cases if not c.get("success", False))
    print(f"wrote {len(cases)} runs ({failed} failed) to {path}")


def model_available(provider: str) -> bool:
    return bool(os.environ.get({"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}[provider]))
