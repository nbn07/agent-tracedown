"""``tracedown``: one command to see Tracedown work.

    tracedown up              # server + web app on http://127.0.0.1:7070, with demo runs, opens the browser
    tracedown up --no-demo    # the same, starting empty, for your own runs
    tracedown demo            # print the demo diagnoses in the terminal, no server

``tracedown-server`` stays the command for production (Postgres, Redis, several workers).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser


def _wait_until_up(url: str, server, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if getattr(server, "should_exit", False):
            return False
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    return False


def _has_runs(url: str) -> bool:
    with urllib.request.urlopen(url + "/api/v1/runs?limit=1", timeout=10) as r:
        return bool(json.loads(r.read()).get("runs"))


def _port_free(host: str, port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


# Folders kept in sync by a cloud drive. Their sync clients lock files while uploading, and
# SQLite then fails with "attempt to write a readonly database".
_SYNCED = ("/library/cloudstorage/", "/onedrive", "/dropbox", "/google drive", "/mobile documents/", "/icloud")


def default_db() -> str:
    """A database next to the user's home, not the current folder (which may be synced)."""
    home = os.path.join(os.path.expanduser("~"), ".tracedown")
    return os.path.join(home, "tracedown.db")


def synced_folder(path: str) -> bool:
    p = os.path.abspath(path).replace("\\", "/").lower()
    return any(m in p for m in _SYNCED)


def up(args: argparse.Namespace) -> int:
    args.db = os.path.expanduser(args.db)
    if args.db != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(args.db)), exist_ok=True)
        if synced_folder(args.db):
            print(f"Warning: {args.db} is in a cloud-synced folder (OneDrive, iCloud, Dropbox or Google Drive).\n"
                  "Sync can lock the file and make writes fail. Use a local path, for example "
                  "--db ~/.tracedown/tracedown.db", file=sys.stderr)
    os.environ["TDN_DB"] = args.db
    import uvicorn

    from .api import create_app, web_available

    if not _port_free(args.host, args.port):
        print(f"Port {args.port} is in use. Try: tracedown up --port {args.port + 1}", file=sys.stderr)
        return 1
    local = "127.0.0.1" if args.host in ("0.0.0.0", "::", "") else args.host  # where this process reaches itself
    url = f"http://{local}:{args.port}"
    app = create_app()
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    if not _wait_until_up(url, server):
        print("The server did not start. Run `tracedown-server` to see the error.", file=sys.stderr)
        return 1

    if not args.no_demo and not _has_runs(url):
        print("Loading the demo runs …", flush=True)
        from .demo.runner import load
        load(url, verbose=False)
    if args.read_only:
        app.state.read_only = True  # after loading the demo: from now on, nobody can write

    page = url + "/runs/" if web_available() else url + "/"
    where = f"   (listening on {args.host}:{args.port})" if local != args.host else ""
    print(f"\nTracedown is running at {url}{where}   (data: {os.path.abspath(args.db)})")
    if args.read_only:
        print("Read-only: every write is refused from now on.")
    if not web_available():
        print("This install has no bundled web app, so you get the simple viewer. For the full one: "
              "cd web && npm install && npm run dev")
    print("Press Ctrl+C to stop.\n")
    if not args.no_open:
        try:
            webbrowser.open(page)
        except Exception:  # noqa: BLE001 - no browser (SSH, containers): the URL is printed above
            pass
    try:
        while thread.is_alive():
            thread.join(0.5)
    except KeyboardInterrupt:
        server.should_exit = True
        thread.join(5)
    return 0


def demo(args: argparse.Namespace) -> int:
    from .demo.runner import main
    main([])
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="tracedown", description="Tracedown: find the step that caused your AI agent's failure.")
    sub = p.add_subparsers(dest="command", required=True)

    u = sub.add_parser("up", help="start the server and web app, with demo runs, and open the browser")
    u.add_argument("--host", default=os.environ.get("TDN_HOST", "127.0.0.1"))
    u.add_argument("--port", type=int, default=int(os.environ.get("TDN_PORT", "7070")))
    u.add_argument("--db", default=os.environ.get("TDN_DB") or default_db(),
                   help="SQLite file (default: ~/.tracedown/tracedown.db)")
    u.add_argument("--no-demo", action="store_true", help="do not load the demo runs into an empty database")
    u.add_argument("--no-open", action="store_true", help="do not open a browser")
    u.add_argument("--read-only", action="store_true",
                   help="refuse every write once the demo runs are loaded (for a public demo)")
    u.set_defaults(func=up)

    d = sub.add_parser("demo", help="print the demo agents' diagnoses in the terminal, no server")
    d.set_defaults(func=demo)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
