#!/usr/bin/env python3
"""Serve the moqx metrics dashboard and proxy its scrape targets same-origin.

The dashboard fetches /metrics cross-origin, so every target normally has to
send Access-Control-Allow-Origin. A relay bound to localhost sends none and
isn't reachable anyway. Serving the page and proxying the targets from one
origin removes the CORS question entirely.

Routes:
  /                dashboard HTML
  /relay/metrics   -> relay admin  (app endpoint field)
  /metrics         -> node_exporter (so the "host port" field can be this port)

Pair with an ssh tunnel, e.g.
  ssh -N -L 18000:localhost:8000 -L 19100:localhost:9100 <relay-host>
"""
import argparse
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARGS = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _proxy(self, upstream):
        try:
            with urllib.request.urlopen(upstream, timeout=10) as r:
                body = r.read()
                ctype = r.headers.get("Content-Type", "text/plain; charset=utf-8")
        except urllib.error.HTTPError as e:
            body, ctype = f"upstream {e.code}: {upstream}".encode(), "text/plain"
            self._send(e.code, body, ctype)
            return
        except Exception as e:
            body = f"upstream unreachable: {upstream}\n{e}".encode()
            self._send(502, body, "text/plain")
            return
        self._send(200, body, ctype)

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html", "/dash.html"):
            try:
                body = open(ARGS.dash, "rb").read()
            except OSError as e:
                self._send(500, f"cannot read {ARGS.dash}: {e}".encode(), "text/plain")
                return
            self._send(200, body, "text/html; charset=utf-8")
        elif path.startswith("/relay/"):
            self._proxy(ARGS.relay.rstrip("/") + path[len("/relay"):])
        elif path == "/metrics":
            self._proxy(ARGS.node.rstrip("/") + "/metrics")
        elif path == "/healthz":
            self._send(200, b"ok\n", "text/plain")
        else:
            self._send(404, b"not found\n", "text/plain")

    def log_message(self, fmt, *a):
        pass


def main():
    global ARGS
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--dash", required=True, help="path to metrics-dashboard.html")
    p.add_argument("--relay", default="http://127.0.0.1:18000",
                   help="relay admin base URL (tunnelled)")
    p.add_argument("--node", default="http://127.0.0.1:19100",
                   help="node_exporter base URL (tunnelled)")
    ARGS = p.parse_args()

    for name, url in (("relay", ARGS.relay), ("node", ARGS.node)):
        try:
            urllib.request.urlopen(url + "/metrics", timeout=5).read(64)
            print(f"  {name:6s} {url} reachable")
        except Exception as e:
            print(f"  {name:6s} {url} NOT reachable: {e}", file=sys.stderr)

    srv = ThreadingHTTPServer((ARGS.bind, ARGS.port), Handler)
    print(f"\ndashboard   http://{ARGS.bind}:{ARGS.port}/")
    print(f"app endpoint field   http://<this-host>:{ARGS.port}/relay/metrics")
    print(f"host port field      {ARGS.port}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
