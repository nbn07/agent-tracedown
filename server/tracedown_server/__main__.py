"""``python -m tracedown_server`` / ``tracedown-server``: run the dev profile (SQLite, one process)."""
import argparse
import os


def main() -> None:
    p = argparse.ArgumentParser(prog="tracedown-server", description="Tracedown server (dev profile)")
    p.add_argument("--host", default=os.environ.get("TDN_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("TDN_PORT", "7070")))
    p.add_argument("--db", default=os.environ.get("TDN_DB", "agent_tracedown.db"),
                   help="SQLite file (dev profile); ':memory:' for ephemeral")
    args = p.parse_args()
    os.environ["TDN_DB"] = args.db

    import uvicorn
    from .api import create_app

    print(f"Tracedown listening on http://{args.host}:{args.port}  (db: {args.db})")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
