"""Hallucination investigator (v1.1): claim-level attribution.

Pipeline for one run:

1. **Split** the final answer into claims (sentences, then compound clauses), keeping
   character offsets so the UI can paint them.
2. **Gather evidence units**: every retrieved chunk, every successful tool result,
   memory reads and the user's own message, each with the step that produced it.
3. **Attribute**: score each claim against each unit (weighted term recall; numbers
   and names count double) and keep the best supporting units.
4. **Label**: supported, partial, unsupported, contradicted (a close unit states a
   different number), stale (supported only by superseded sources), or no_claim
   (pleasantries such as "Let me know if you need anything else").
5. **Locate the origin**: the earliest LLM step whose output already contained the claim.
6. **Heatmap**: per-token support over the answer text.

An optional ``judge(claim, evidence_texts) -> {"label", "rationale"}`` refines
partial and unsupported claims with an entailment model. Scores are lexical
heuristics, reported as a score, not a calibrated probability.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

Judge = Callable[[str, List[str]], Dict[str, str]]

_judge: Optional[Judge] = None


def set_judge(judge: Optional[Judge]) -> None:
    """Install an entailment judge (e.g. an LLM call). ``None`` disables it."""
    global _judge
    _judge = judge


# ----------------------------------------------------------------------------- text utilities

STOP = set("""
a an the and or but if then than that this these those with from into onto over under for of to in on at by
is are was were be been being am do does did done have has had having will would shall should can could may might
must it its it's you your yours we our ours they them their i me my he she his her as so not no yes also just only
very more most some any all each such what when where which who whom whose why how there here about after before
while because until again further once both few other own same too s t now let know need please thanks thank per
""".split())

_TOKEN = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?|[A-Za-z][A-Za-z'-]*")
_NUMERIC = re.compile(r"^\$?\d")


def _stem(w: str) -> str:
    w = w.lower().strip("'-")
    for suf in ("ies", "ing", "ed", "es", "s"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def _norm_num(tok: str) -> str:
    t = tok.strip("$%").replace(",", "")
    try:
        f = float(t)
        return str(int(f)) if f == int(f) else str(f)
    except ValueError:
        return t


def terms(text: str) -> List[Tuple[str, int, int, str]]:
    """Tokens as (normalized, start, end, type) with type in {num, name, word, stop}."""
    out = []
    for m in _TOKEN.finditer(text):
        raw = m.group(0)
        if _NUMERIC.match(raw):
            out.append((_norm_num(raw), m.start(), m.end(), "num"))
            continue
        for part in re.split(r"[_-]", raw):
            if not part:
                continue
            low = part.lower()
            if low in STOP or len(low) < 3:
                kind = "stop"
            elif part[0].isupper() and m.start() > 0 and text[m.start() - 2:m.start()].strip() not in (".", "!", "?", ""):
                kind = "name"
            else:
                kind = "word"
            out.append((_stem(low), m.start(), m.end(), kind))
    return out


# ----------------------------------------------------------------------------- claims

# A period ends a sentence only before whitespace or the end: "$4.2 billion", "example.com" and
# "e.g." stay whole.
_SENT = re.compile(r"(?:[^.!?\n]|\.(?=\S))+(?:[.!?]+|$)", re.M)
_CLAUSE = re.compile(r";\s+|,\s+(?:and|but|while|whereas)\s+|\s+and\s+(?=(?:includes?|offers?|has|have|is|are|was|"
                     r"were|will|costs?|provides?|supports?|gives?|requires?|takes?)\b)")


# Sentences that offer help or close the conversation state nothing to verify.
_COURTESY = re.compile(r"\b(let (me|us) know|feel free|anything else|happy to help|glad (I|to) (could )?help|"
                       r"how (else )?can I (help|assist)|is there anything|have a (great|nice|good) (day|trip)|"
                       r"don't hesitate|thank you for (contacting|reaching|your patience)|I hope)\b", re.I)


@dataclass
class Claim:
    id: str
    text: str
    start: int
    end: int


def split_claims(answer: str) -> List[Claim]:
    claims: List[Claim] = []
    for m in _SENT.finditer(answer):
        sent = m.group(0)
        if not sent.strip():
            continue
        base = m.start()
        seps = list(_CLAUSE.finditer(sent))
        bounds = [0] + [x for m2 in seps for x in (m2.start(), m2.end())] + [len(sent)]
        pieces = [(bounds[i], bounds[i + 1]) for i in range(0, len(bounds), 2)]
        # Keep a clause separate only if both sides carry content; otherwise merge back.
        merged: List[Tuple[int, int]] = []
        for s, e in pieces:
            content = [t for t in terms(sent[s:e]) if t[3] != "stop"]
            if merged and len(content) < 2:
                merged[-1] = (merged[-1][0], e)  # re-joins the separator too
            else:
                merged.append((s, e))
        for s, e in merged:
            seg = sent[s:e]
            lead = len(seg) - len(seg.lstrip())
            text = seg.strip()
            if text:
                claims.append(Claim(f"c{len(claims) + 1}", text, base + s + lead, base + s + lead + len(text)))
    return claims


# ----------------------------------------------------------------------------- evidence

@dataclass
class Unit:
    id: str
    kind: str          # chunk | tool | memory | input
    text: str
    step: int
    span_id: str
    source: Optional[str] = None
    stale: bool = False
    weight: float = 1.0   # user input counts as weak evidence
    _terms: List[Tuple[str, int, int, str]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self._terms = terms(self.text)
        self.vocab = {t[0] for t in self._terms if t[3] != "stop"}
        self.nums = {t[0] for t in self._terms if t[3] == "num"}


def _flatten(value: Any, prefix: str = "") -> List[str]:
    if isinstance(value, dict):
        out = []
        for k, v in value.items():
            out += _flatten(v, f"{prefix}{k}: " if not isinstance(v, (dict, list)) else f"{prefix}{k} ")
        return out
    if isinstance(value, list):
        return [x for v in value for x in _flatten(v, prefix)]
    if isinstance(value, str) and value[:1] in "{[":
        try:
            return _flatten(json.loads(value), prefix)
        except ValueError:
            pass
    return [f"{prefix}{value}"]


def evidence_units(run) -> List[Unit]:
    """``run`` is an ``rca.RunView``."""
    units: List[Unit] = []
    for s in run.spans:
        step, a = run.step(s), s["attributes"]
        if s["kind"] == "retrieval":
            for i, c in enumerate(run.output(s) or []):
                if isinstance(c, dict) and c.get("text"):
                    units.append(Unit(str(c.get("id") or f"chunk{i}@{step}"), "chunk", c["text"], step, s["span_id"],
                                      source=c.get("source"), stale=bool(c.get("stale"))))
        elif s["kind"] == "tool" and s["status"]["code"] != "ERROR" and s.get("output_digest"):
            name = a.get("gen_ai.tool.name", s["name"])
            # The call's arguments are context for its result ("plan: pro" -> "usd: 39").
            text = "\n".join([name] + _flatten(run.input(s)) + _flatten(run.output(s)))
            units.append(Unit(f"tool:{name}@{step}", "tool", text, step, s["span_id"], source=name))
        elif s["kind"] == "memory" and a.get("tdn.memory.op") == "read" and s.get("output_digest"):
            key = a.get("tdn.memory.key")
            units.append(Unit(f"memory:{key}@{step}", "memory", f"{key}: {run.output(s)}", step, s["span_id"],
                              source=key))
        elif s["kind"] == "llm" and s.get("input_digest"):
            # Multi-turn agents: what the user said later in the conversation is evidence too
            # (weaker than a tool result, like the first message).
            said = [m.get("content") for m in (run.input(s) if isinstance(run.input(s), list) else [])
                    if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str)]
            if said:
                units.append(Unit(f"conversation@{step}", "input", "\n".join(said), step, s["span_id"],
                                  source="user", weight=0.6))
    if run.root.get("input_digest"):
        inp = run.input(run.root)
        units.append(Unit("user_input", "input", "\n".join(_flatten(inp)), 0, run.root["span_id"],
                          source="user", weight=0.6))
    return units


# ----------------------------------------------------------------------------- attribution

W = {"num": 2.0, "name": 2.0, "word": 1.0}


def _score(claim_terms, unit: Unit) -> Tuple[float, List[str]]:
    content = [t for t in claim_terms if t[3] != "stop"]
    if not content:
        return 0.0, []
    total = sum(W[t[3]] for t in content)
    hit = [t for t in content if t[0] in unit.vocab]
    return sum(W[t[3]] for t in hit) / total * unit.weight, sorted({t[0] for t in hit})


def investigate(run, judge: Optional[Judge] = None) -> Dict[str, Any]:
    judge = judge if judge is not None else _judge
    from ..rca.detectors import text_of  # local import: rca imports this module too

    answer = text_of(run.output(run.root))
    units = evidence_units(run)
    llm_outputs = [(run.step(s), s["span_id"], text_of(run.output(s))) for s in run.of_kind("llm")]
    claims_out, segments = [], []

    for c in split_claims(answer or ""):
        ct = terms(c.text)
        content = [t for t in ct if t[3] != "stop"]
        nums = {t[0] for t in ct if t[3] == "num"}
        scored = sorted(((*_score(ct, u), u) for u in units), key=lambda x: -x[0])
        best_score, best_terms, best = scored[0] if scored else (0.0, [], None)
        if len(scored) >= 2 and scored[1][0] > 0:
            # A claim may combine two sources; score it against their union, slightly discounted.
            union = Unit("union", "union", scored[0][2].text + "\n" + scored[1][2].text, 0, "")
            union.weight = min(scored[0][2].weight, scored[1][2].weight)
            best_score = max(best_score, 0.9 * _score(ct, union)[0])
        supporting = [{"unit": u.id, "kind": u.kind, "source": u.source, "step": u.step, "span_id": u.span_id,
                       "score": round(sc, 2), "matched": mt, "stale": u.stale}
                      for sc, mt, u in scored[:3] if sc > 0]

        # Numbers must come from a source about the same thing, not from any source anywhere
        # ("9" in "Office hours are 9 to 5" does not support "$9 per month").
        topical = [u for sc, _, u in scored if sc >= 0.35] or units
        missing_nums = sorted(n for n in nums if not any(n in u.nums for u in topical))
        rationale = ""
        if (len(content) <= 2 and not nums) or (_COURTESY.search(c.text) and not nums):
            label = "no_claim"
        elif missing_nums and best is not None and best_score >= 0.4 and best.nums - nums:
            label = "contradicted"
            rationale = (f"states {', '.join(missing_nums)} but {best.id} (step {best.step}) "
                         f"says {', '.join(sorted(best.nums - nums))}")
        elif missing_nums:
            label = "unsupported"
            rationale = f"number(s) {', '.join(missing_nums)} appear in no source"
        elif best_score >= 0.6:
            label = "stale" if best is not None and best.stale and not any(
                s["score"] >= 0.6 and not s["stale"] for s in supporting) else "supported"
            if label == "stale":
                rationale = f"only supported by superseded source {best.source}"
        elif best_score >= 0.35:
            label = "partial"
            rationale = f"{best_score:.0%} of its key terms appear in {best.id}"
        else:
            label = "unsupported"
            rationale = "no retrieved chunk or tool result contains its key terms"

        if judge and label in ("partial", "unsupported"):
            verdict = judge(c.text, [u.text for _, _, u in scored[:3]])
            if verdict.get("label") in ("supported", "partial", "unsupported", "contradicted"):
                label, rationale = verdict["label"], f"judge: {verdict.get('rationale', '')}".strip()

        # Origin: earliest LLM output that already contained (most of) the claim.
        origin = None
        for step, span_id, out in llm_outputs:
            ot = {t[0] for t in terms(out) if t[3] != "stop"}
            if c.text in out or (content and sum(t[0] in ot for t in content) / len(content) >= 0.8):
                origin = {"step": step, "span_id": span_id}
                break

        support_value = {"supported": 1.0, "stale": 0.5, "partial": 0.5, "no_claim": None,
                         "unsupported": 0.0, "contradicted": 0.0}[label]
        claims_out.append({
            "id": c.id, "text": c.text, "start": c.start, "end": c.end, "label": label,
            "score": round(best_score, 2), "rationale": rationale, "evidence": supporting, "origin": origin,
        })
        # Heatmap: each token of the claim, coloured by whether the best unit backs it.
        vocab = best.vocab if best is not None else set()
        for norm, s, e, kind in ct:
            if kind == "stop" or support_value is None:
                tok = None
            elif kind == "num":
                tok = 1.0 if norm in (best.nums if best else set()) else 0.0
            else:
                tok = 1.0 if norm in vocab else 0.0
            segments.append({"start": c.start + s, "end": c.start + e, "claim": c.id, "support": tok})

    factual = [c for c in claims_out if c["label"] != "no_claim"]
    bad = sum(c["label"] in ("unsupported", "contradicted") for c in factual)
    half = sum(c["label"] in ("partial", "stale") for c in factual)
    return {
        "answer": answer,
        "claims": claims_out,
        "segments": segments,
        "summary": {
            "claims": len(factual),
            "supported": sum(c["label"] == "supported" for c in factual),
            "partial": sum(c["label"] == "partial" for c in factual),
            "stale": sum(c["label"] == "stale" for c in factual),
            "unsupported": sum(c["label"] == "unsupported" for c in factual),
            "contradicted": sum(c["label"] == "contradicted" for c in factual),
            # Share of factual claims without support (partial counts half). Not a calibrated probability.
            "hallucination_score": round((bad + 0.5 * half) / len(factual), 2) if factual else 0.0,
            "evidence_units": len(units),
            "judge": judge is not None,
        },
    }
