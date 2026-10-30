"""Live harness 2: a LangGraph ReAct SQL agent on the Chinook database, traced by Tracedown.

The agent is LangGraph's prebuilt ``create_react_agent`` (the pattern of LangChain's SQL
agent tutorial) with three tools: ``list_tables``, ``describe_table`` and ``run_query``
over a read-only SQLite connection. Each question has a gold SQL query; the run passes
when the gold value appears in the final answer.

Labels (category ``wrong_answer``, see LABELING.md "Live harnesses"), mechanical and weaker
than τ-bench's, so check a sample by hand:

* no query result ever contained the gold value → the cause is the last ``run_query``
  (the agent asked the wrong question of the database);
* a query result contained it but the answer does not → the cause is the final model turn.

    pip install langgraph langchain-openai     # or langchain-anthropic
    git clone https://github.com/lerocha/chinook-database ../chinook-database
    export OPENAI_API_KEY=...
    python realworld/live/langgraph_sql_agent.py --db ../chinook-database/ChinookDatabase/DataSources/Chinook_Sqlite.sqlite \\
        --model openai:gpt-4.1-mini --repeats 3
    python realworld/score.py --file realworld/data/live-lg-sql-openai_gpt-4.1-mini.jsonl

``--fake`` uses a scripted chat model (no key) whose canned SQL is wrong for some questions.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
from typing import Any, Dict, List

from common import ROOT, exporter, write_cases

QUESTIONS: List[Dict[str, str]] = [
    {"id": "n-customers", "q": "How many customers are there?", "gold": "SELECT COUNT(*) FROM Customer"},
    {"id": "n-tracks", "q": "How many tracks are in the database?", "gold": "SELECT COUNT(*) FROM Track"},
    {"id": "top-country", "q": "Which country has the most customers?",
     "gold": "SELECT Country FROM Customer GROUP BY Country ORDER BY COUNT(*) DESC LIMIT 1"},
    {"id": "usa-sales", "q": "What is the total of all invoices billed to the USA? Give the amount.",
     "gold": "SELECT ROUND(SUM(Total), 2) FROM Invoice WHERE BillingCountry = 'USA'"},
    {"id": "top-artist-albums", "q": "Which artist has the most albums?",
     "gold": "SELECT ar.Name FROM Artist ar JOIN Album al ON al.ArtistId = ar.ArtistId GROUP BY ar.ArtistId ORDER BY COUNT(*) DESC LIMIT 1"},
    {"id": "rock-tracks", "q": "How many tracks belong to the Rock genre?",
     "gold": "SELECT COUNT(*) FROM Track t JOIN Genre g ON g.GenreId = t.GenreId WHERE g.Name = 'Rock'"},
    {"id": "longest-track", "q": "What is the name of the longest track?",
     "gold": "SELECT Name FROM Track ORDER BY Milliseconds DESC LIMIT 1"},
    {"id": "top-agent", "q": "Which sales support agent (first and last name) has the highest total sales from their customers?",
     "gold": "SELECT e.FirstName || ' ' || e.LastName FROM Employee e JOIN Customer c ON c.SupportRepId = e.EmployeeId "
             "JOIN Invoice i ON i.CustomerId = c.CustomerId GROUP BY e.EmployeeId ORDER BY SUM(i.Total) DESC LIMIT 1"},
    {"id": "n-playlists-90s", "q": "How many tracks are on the playlist named '90’s Music'?",
     "gold": "SELECT COUNT(*) FROM PlaylistTrack pt JOIN Playlist p ON p.PlaylistId = pt.PlaylistId WHERE p.Name = '90’s Music'"},
    {"id": "best-year", "q": "In which year was invoice revenue highest?",
     "gold": "SELECT strftime('%Y', InvoiceDate) FROM Invoice GROUP BY 1 ORDER BY SUM(Total) DESC LIMIT 1"},
    {"id": "top-customer", "q": "Which customer (first and last name) has spent the most in total?",
     "gold": "SELECT c.FirstName || ' ' || c.LastName FROM Customer c JOIN Invoice i ON i.CustomerId = c.CustomerId "
             "GROUP BY c.CustomerId ORDER BY SUM(i.Total) DESC LIMIT 1"},
    {"id": "avg-track-sec", "q": "What is the average track length in seconds, rounded to the nearest whole second?",
     "gold": "SELECT CAST(ROUND(AVG(Milliseconds) / 1000.0) AS INTEGER) FROM Track"},
]

SYSTEM = ("You are an agent that answers questions about a SQLite music-store database. First look at the tables, "
          "then the schema of the relevant ones, then run a query. Never modify the database. Answer with the value.")


def make_tools(db: str):
    from langchain_core.tools import tool
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, check_same_thread=False)

    @tool
    def list_tables() -> str:
        """List the tables in the database."""
        return ", ".join(r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))

    @tool
    def describe_table(table: str) -> str:
        """Show the columns of one table."""
        rows = conn.execute(f"PRAGMA table_info('{table.replace(chr(39), '')}')").fetchall()
        if not rows:
            raise ValueError(f"no such table: {table}")
        return ", ".join(f"{r[1]} {r[2]}" for r in rows)

    @tool
    def run_query(sql: str) -> str:
        """Run a read-only SQL query and return up to 20 rows."""
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description or []]
        return json.dumps({"columns": cols, "rows": cur.fetchmany(20)}, default=str)

    return [list_tables, describe_table, run_query], conn


def norm(s: str) -> str:
    return re.sub(r"[\s,$]", "", str(s)).lower()


def contains(text: str, gold: Any) -> bool:
    t = norm(text)
    cands = {norm(gold)}
    if isinstance(gold, float):
        cands |= {norm(f"{gold:.2f}"), norm(f"{gold:.1f}") if gold * 10 == int(gold * 10) else norm(f"{gold:.2f}")}
    return any(c and c in t for c in cands)


# ----------------------------------------------------------------------------- scripted model (--fake)

FAKE_SQL = {  # deliberately wrong for some questions: the bugs a real model makes
    "n-customers": "SELECT COUNT(*) FROM Customer",
    "n-tracks": "SELECT COUNT(*) FROM Track",
    "top-country": "SELECT Country FROM Customer GROUP BY Country ORDER BY COUNT(*) DESC LIMIT 1",
    "usa-sales": "SELECT ROUND(SUM(Total), 2) FROM Invoice WHERE BillingCountry = 'United States'",  # wrong value
    "top-artist-albums": "SELECT ar.Name FROM Artist ar JOIN Album al ON al.ArtistId = ar.ArtistId GROUP BY ar.ArtistId ORDER BY COUNT(*) DESC LIMIT 1",
    "rock-tracks": "SELECT COUNT(*) FROM Track WHERE GenreId = 2",  # wrong id (2 is Jazz)
    "longest-track": "SELECT Name FROM Track ORDER BY Milliseconds DESC LIMIT 1",
    "top-agent": "SELECT e.FirstName || ' ' || e.LastName FROM Employee e JOIN Customer c ON c.SupportRepId = e.EmployeeId GROUP BY e.EmployeeId ORDER BY COUNT(*) DESC LIMIT 1",  # counts customers
    "n-playlists-90s": "SELECT COUNT(*) FROM PlaylistTrack pt JOIN Playlist p ON p.PlaylistId = pt.PlaylistId WHERE p.Name = '90’s Music'",
    "best-year": "SELECT strftime('%Y', InvoiceDate) FROM Invoice GROUP BY 1 ORDER BY SUM(Total) DESC LIMIT 1",
    "top-customer": "SELECT c.FirstName || ' ' || c.LastName FROM Customer c JOIN Invoice i ON i.CustomerId = c.CustomerId GROUP BY c.CustomerId ORDER BY SUM(i.Total) DESC LIMIT 1",
    "avg-track-sec": "SELECT AVG(Milliseconds) FROM Track",  # milliseconds, not seconds
}


def make_fake_model():
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    by_q = {q["q"]: q["id"] for q in QUESTIONS}

    class ScriptedSQLModel(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "scripted-sql"

        def bind_tools(self, tools, **kw):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kw):
            qid = next((by_q[m.content] for m in messages if getattr(m, "type", "") == "human" and m.content in by_q), None)
            tools_done = [m for m in messages if isinstance(m, ToolMessage)]
            n = len(tools_done)
            if n == 0:
                msg = AIMessage(content="", tool_calls=[{"name": "list_tables", "args": {}, "id": f"c{n}"}])
            elif n == 1:
                table = re.search(r"FROM (\w+)", FAKE_SQL[qid]).group(1)
                msg = AIMessage(content="", tool_calls=[{"name": "describe_table", "args": {"table": table}, "id": f"c{n}"}])
            elif n == 2:
                msg = AIMessage(content="", tool_calls=[{"name": "run_query", "args": {"sql": FAKE_SQL[qid]}, "id": f"c{n}"}])
            else:
                rows = json.loads(tools_done[-1].content).get("rows") or [[None]]
                msg = AIMessage(content=f"The answer is {rows[0][0]}.")
            return ChatResult(generations=[ChatGeneration(message=msg)])

    return ScriptedSQLModel()


def make_model(spec: str):
    provider, _, model = spec.partition(":")
    if provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model, temperature=0)
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model, temperature=0)
    raise SystemExit("use --model openai:<model> or anthropic:<model>")


# ----------------------------------------------------------------------------- labelling

def label_run(spans, blobs, gold, final) -> Dict[str, Any]:
    order = sorted(spans, key=lambda s: s["attributes"]["tdn.step"])
    queries = [s for s in order if s["kind"] == "tool" and s["attributes"].get("gen_ai.tool.name") == "run_query"]
    llm = [s for s in order if s["kind"] == "llm"]
    seen = any(contains(blobs.get(s.get("output_digest"), ""), gold) for s in queries)
    if not seen and queries:
        target, detail = queries[-1], "no query result contained the gold value"
    else:
        target, detail = (llm[-1] if llm else None), ("the gold value was retrieved but not reported" if seen
                                                      else "no query was run")
    step = target["attributes"]["tdn.step"] if target else None
    before = [s for s in llm if step is not None and s["attributes"]["tdn.step"] < step]
    return {"category": "wrong_answer", "step": step, "detail": detail, "expected": [str(gold)],
            "emitted_by_step": before[-1]["attributes"]["tdn.step"] if before and target is not llm[-1] else None,
            "answer": (final or "")[:300]}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True, help="Chinook_Sqlite.sqlite")
    ap.add_argument("--model", default="openai:gpt-4.1-mini")
    ap.add_argument("--fake", action="store_true")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--endpoint")
    ap.add_argument("--api-key")
    a = ap.parse_args()

    from langgraph.prebuilt import create_react_agent
    from agent_tracedown.integrations import langchain as tdn_lc

    mem = exporter(a.endpoint, a.api_key, project="langgraph-sql")
    tools, conn = make_tools(a.db)
    llm = make_fake_model() if a.fake else make_model(a.model)
    graph = create_react_agent(llm, tools, prompt=SYSTEM)
    name = "fake" if a.fake else a.model.replace(":", "_").replace("/", "_")
    cases = []
    for r in range(a.repeats):
        for q in QUESTIONS:
            gold = conn.execute(q["gold"]).fetchone()[0]
            handler = tdn_lc.callback("SQL agent", **{"tdn.realworld.question": q["id"]})
            final, error = None, None
            try:
                out = graph.invoke({"messages": [("user", q["q"])]},
                                   config={"callbacks": [handler], "recursion_limit": 25})
                final = out["messages"][-1].content
                final = final if isinstance(final, str) else json.dumps(final)
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
            tid = handler.trace_ids[0]
            spans, blobs = mem.trace(tid), dict(mem.blobs)
            ok = error is None and contains(final, gold)
            root = next(s for s in spans if s["attributes"].get("tdn.run.root"))
            root["attributes"]["tdn.tools.available"] = [{"name": t.name, "side_effects": False} for t in tools]
            label = None
            if not ok:
                root["status"] = {"code": "ERROR", "message": error or "answer does not match the gold value"}
                root["attributes"]["tdn.run.outcome"] = "failure"
                label = label_run(spans, blobs, gold, final)
            used = {d for s in spans for d in (s.get("input_digest"), s.get("output_digest")) if d}
            bundle = {"spans": spans, "blobs": {d: blobs[d] for d in used if d in blobs}, "write_tools": []}
            cases.append({"id": f"lg-sql-{name}-{q['id']}-r{r}", "model": name, "question": q["q"], "gold": str(gold),
                          "success": ok, "label": label, "bundle": bundle})
            print(f"{q['id']:<20} {'ok' if ok else 'FAIL @' + str(label['step']) + ' ' + label['detail']}")
    write_cases(os.path.join(ROOT, "realworld", "data", f"live-lg-sql-{name}.jsonl"), cases)


if __name__ == "__main__":
    main()
