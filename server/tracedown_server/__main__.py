"""``python -m tracedown_server`` / ``tracedown-server``.

Dev profile: one process, SQLite (``--db``). Production profile: ``--database-url`` (Postgres),
``--redis-url`` (shared cache and live feed) and ``--workers N``; any number of replicas behind
a load balancer then behave as one server.
"""
import argparse
import os


def main() -> None:
    p = argparse.ArgumentParser(prog="tracedown-server", description="Tracedown server")
    p.add_argument("--host", default=os.environ.get("TDN_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("TDN_PORT", "7070")))
    p.add_argument("--db", default=os.environ.get("TDN_DB", "agent_tracedown.db"),
                   help="SQLite file (dev profile); ':memory:' for ephemeral")
    p.add_argument("--database-url", default=os.environ.get("TDN_DATABASE_URL"),
                   help="postgresql://user:pass@host:5432/db (production metadata store)")
    p.add_argument("--redis-url", default=os.environ.get("TDN_REDIS_URL"),
                   help="redis://host:6379/0 (shared cache, live feed and rate limits across replicas)")
    p.add_argument("--workers", type=int, default=int(os.environ.get("TDN_WORKERS", "1")),
                   help="server processes; more than 1 needs --database-url and --redis-url")
    args = p.parse_args()
    os.environ["TDN_DB"] = args.db
    if args.database_url:
        os.environ["TDN_DATABASE_URL"] = args.database_url
    if args.redis_url:
        os.environ["TDN_REDIS_URL"] = args.redis_url
    if args.workers > 1 and not (args.database_url and args.redis_url):
        p.error("--workers > 1 needs --database-url (Postgres) and --redis-url: SQLite and the in-process "
                "live feed are per process")

    import uvicorn

    store = "Postgres" if args.database_url else f"SQLite {args.db}"
    extra = ", Redis" if args.redis_url else ""
    print(f"Tracedown listening on http://{args.host}:{args.port}  ({store}{extra}, {args.workers} worker(s))")
    if args.workers > 1:
        uvicorn.run("tracedown_server.api:create_app", factory=True, host=args.host, port=args.port,
                    workers=args.workers, log_level="warning")
    else:
        from .api import create_app
        uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
