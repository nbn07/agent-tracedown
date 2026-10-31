"""A ClickHouse HTTP-interface server backed by the real ClickHouse engine (chdb).

    python server/tests/integration/chdb_http.py --port 18123 --path /tmp/ch1

Used by the integration tests when no ``clickhouse-server`` binary is available. The SQL
engine, MergeTree storage, TTL and every query are real ClickHouse (chdb embeds it); only
the HTTP front end is this file. It implements the subset of the HTTP interface that
Tracedown's ``HttpClient`` uses: ``POST /?query=...`` with an optional body (data for
INSERT, or the query itself), errors as HTTP 500 with ClickHouse's message, and
``GET /ping``. One process per data directory: chdb allows one session per process.
"""
import argparse
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def serve(port: int, path: str) -> None:
    from chdb import session
    sess = session.Session(path)
    lock = threading.Lock()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def _run(self, body: bytes) -> None:
            u = urllib.parse.urlparse(self.path)
            if u.path == "/ping":
                return self._send(200, b"Ok.\n")
            q = urllib.parse.parse_qs(u.query).get("query", [""])[0]
            sql = (q + ("\n" + body.decode() if body else "")) if q else body.decode()
            with lock:
                try:
                    res = sess.query(sql)
                    if getattr(res, "has_error", lambda: False)():
                        return self._send(500, res.error_message().encode())
                    out = res.bytes() if hasattr(res, "bytes") else str(res).encode()
                except Exception as e:  # noqa: BLE001
                    return self._send(500, str(e).encode())
            self._send(200, out)

        def _send(self, code: int, data: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-ClickHouse-Server-Display-Name", "chdb-http")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self._run(b"")

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            self._run(self.rfile.read(n) if n else b"")

    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18123)
    ap.add_argument("--path", required=True)
    a = ap.parse_args()
    serve(a.port, a.path)
