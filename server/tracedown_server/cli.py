"""``tracedown``: one command to see Tracedown work.

    tracedown up              # server + web app on http://127.0.0.1:7070, with demo runs, opens the browser
    tracedown up --no-demo    # the same, starting empty, for your own runs
    tracedown demo            # print the demo diagnoses in the terminal, no server
    tracedown doctor          # check the setup (Python, packages, database, port, judge) and explain problems
    tracedown mcp             # MCP server (stdio) for Claude Code, Cursor and other MCP clients

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

_DEFAULT_PORT = 7070


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


def _has_runs(url: str, token: str = None) -> bool:
    req = urllib.request.Request(url + "/api/v1/runs?limit=1",
                                 headers={"Authorization": f"Bearer {token}"} if token else {})
    with urllib.request.urlopen(req, timeout=10) as r:
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


# ----------------------------------------------------------------------------- doctor

_JUDGE_KEYS = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


def _doctor_python(version_info=None) -> tuple:
    v = version_info or sys.version_info
    line = f"Python {v[0]}.{v[1]}.{v[2]}"
    if (v[0], v[1]) >= (3, 10):
        return True, line
    return False, (f"{line} is too old; Tracedown needs Python 3.10 or newer. "
                   "On a Mac the built-in python3 is 3.9: let uv fetch a newer one, "
                   "`uvx --python 3.12 --from agent-tracedown-server tracedown up`.")


def _doctor_packages() -> list:
    from importlib import metadata
    out = []
    for dist, install in (("agent-tracedown", "agent-tracedown"),
                          ("agent-tracedown-server", "agent-tracedown-server")):
        try:
            out.append((True, f"{dist} {metadata.version(dist)} installed"))
        except metadata.PackageNotFoundError:
            out.append((False, f"{dist} is not installed. Fix: pip install {install}"))
    return out


def _doctor_db(path: str) -> tuple:
    if path == ":memory:":
        return True, "Database :memory: (nothing is kept between runs)"
    path = os.path.abspath(os.path.expanduser(path))
    if synced_folder(path):
        return False, (f"Database {path} is in a cloud-synced folder (OneDrive, iCloud, Dropbox "
                       "or Google Drive). Sync clients lock the file and writes then fail with "
                       "\"attempt to write a readonly database\". Fix: move it to a local path, "
                       "for example --db ~/.tracedown/tracedown.db")
    if os.path.exists(path):
        if not os.path.isfile(path):
            return False, f"Database path {path} exists but is not a file. Fix: pass a file path with --db."
        try:
            with open(path, "ab"):
                pass
        except OSError as e:
            return False, (f"Database {path} exists but is not writable ({e}). "
                           "Fix: check the file permissions, or pass another path with --db.")
        return True, f"Database {path} exists and is writable"
    parent = os.path.dirname(path)
    ancestor = parent
    while not os.path.isdir(ancestor) and os.path.dirname(ancestor) != ancestor:
        ancestor = os.path.dirname(ancestor)
    if os.path.isdir(ancestor) and os.access(ancestor, os.W_OK):
        return True, f"Database {path} does not exist yet; `tracedown up` will create it"
    return False, (f"Database {path} does not exist and {parent} is not a writable folder. "
                   "Fix: pass another path with --db, for example --db ~/.tracedown/tracedown.db")


def _doctor_web() -> tuple:
    from .api import web_available
    if web_available():
        return True, "Bundled web app is present"
    return False, ("This copy has no web app built in (the API still works). "
                   "Fix: build it once with `node web/scripts/build-static.mjs`, "
                   "or reinstall agent-tracedown-server.")


def _doctor_port(host: str, port: int) -> tuple:
    if _port_free(host, port):
        return True, f"Port {port} is free"
    return False, (f"Port {port} is in use. `tracedown up` will take the next free port "
                   f"({port + 1} and up) automatically, or stop the other program.")


def _doctor_judge(env=None) -> tuple:
    env = os.environ if env is None else env
    spec = env.get("TDN_JUDGE")
    if not spec:
        return True, "TDN_JUDGE is not set (the optional LLM judge is off)"
    provider, _, model = spec.partition(":")
    key_var = _JUDGE_KEYS.get(provider)
    if key_var is None or not model:
        return False, (f"TDN_JUDGE={spec!r} is not a spec the judge understands. "
                       "Fix: use openai:<model> or anthropic:<model>.")
    if env.get(key_var):
        return True, f"TDN_JUDGE is set ({provider}:{model}) and {key_var} is set"
    return False, (f"TDN_JUDGE is set ({provider}:{model}) but {key_var} is not set, "
                   f"so the judge cannot call the provider. Fix: export {key_var}=<your key> "
                   "(the key itself is never printed).")


def doctor(args: argparse.Namespace) -> int:
    print("Checking your Tracedown setup:\n")
    checks = [_doctor_python()]
    checks += _doctor_packages()
    checks.append(_doctor_db(args.db))
    checks.append(_doctor_web())
    checks.append(_doctor_port(args.host, args.port))
    checks.append(_doctor_judge())
    problems = 0
    for ok, line in checks:
        print(f"{'✓' if ok else '✗'} {line}")
        problems += 0 if ok else 1
    if problems:
        print(f"\n{problems} problem(s) to fix above; everything else is fine.")
        return 1
    print("\nEverything checks out. Run `tracedown up` to start.")
    return 0


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

    port = args.port
    if port is None:
        port = next((p for p in range(_DEFAULT_PORT, _DEFAULT_PORT + 10) if _port_free(args.host, p)), None)
        if port is None:
            print(f"No free port in {_DEFAULT_PORT}-{_DEFAULT_PORT + 9}. "
                  f"Try: tracedown up --port {_DEFAULT_PORT + 10}", file=sys.stderr)
            return 1
    elif not _port_free(args.host, port):
        print(f"Port {port} is in use. Try: tracedown up --port {port + 1}", file=sys.stderr)
        return 1
    local = "127.0.0.1" if args.host in ("0.0.0.0", "::", "") else args.host  # where this process reaches itself
    url = f"http://{local}:{port}"
    app = create_app()
    from .api import LOCAL_HOSTS
    if args.host in LOCAL_HOSTS:  # on localhost, an open server answers only to local names (DNS rebinding)
        app.state.allowed_hosts = LOCAL_HOSTS
    token = None
    if args.read_only:  # nobody but the demo loader gets in until read-only mode is on
        import secrets
        token = app.state.loading_token = "load-" + secrets.token_hex(16)
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    if not _wait_until_up(url, server):
        print("The server did not start. Run `tracedown-server` to see the error.", file=sys.stderr)
        return 1

    if not args.no_demo and not _has_runs(url, token):
        print("Loading the demo runs …", flush=True)
        from .demo.runner import load
        load(url, verbose=False, api_key=token)
    if args.read_only:
        app.state.read_only = True  # from now on, nobody can write
        app.state.loading_token = None

    page = url + "/runs/" if web_available() else url + "/"
    where = f"   (listening on {args.host}:{port})" if local != args.host else ""
    print(f"\nTracedown is running at {url}{where}   (data: {os.path.abspath(args.db)})")
    if args.read_only:
        print("Read-only: every write is refused from now on.")
    if not web_available():
        print("This copy has no web app built in (the API works). Build it once with: "
              "node web/scripts/build-static.mjs")
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
    u.add_argument("--port", type=int, default=os.environ.get("TDN_PORT"),
                   help="port to use (default: first free port in 7070-7079; TDN_PORT fixes the port)")
    u.add_argument("--db", default=os.environ.get("TDN_DB") or default_db(),
                   help="SQLite file (default: ~/.tracedown/tracedown.db)")
    u.add_argument("--no-demo", action="store_true", help="do not load the demo runs into an empty database")
    u.add_argument("--no-open", action="store_true", help="do not open a browser")
    u.add_argument("--read-only", action="store_true",
                   help="refuse every write once the demo runs are loaded (for a public demo)")
    u.set_defaults(func=up)

    m = sub.add_parser("mcp", help="run Tracedown as an MCP server (stdio) for Claude Code, Cursor and other clients",
                       add_help=False)
    m.set_defaults(func=None)

    d = sub.add_parser("demo", help="print the demo agents' diagnoses in the terminal, no server")
    d.set_defaults(func=demo)

    doc = sub.add_parser("doctor", help="check the setup (Python, packages, database, port, judge) "
                                        "and explain any problem it finds")
    doc.add_argument("--host", default=os.environ.get("TDN_HOST", "127.0.0.1"))
    doc.add_argument("--port", type=int, default=os.environ.get("TDN_PORT") or _DEFAULT_PORT,
                     help="port to check (default: 7070, or TDN_PORT)")
    doc.add_argument("--db", default=os.environ.get("TDN_DB") or default_db(),
                     help="database file to check (default: the one `tracedown up` would use)")
    doc.set_defaults(func=doctor)

    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["mcp"]:  # its own options and --help
        from .mcp_server import main as mcp_main
        return mcp_main(argv[1:])
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
