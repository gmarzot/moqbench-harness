#!/usr/bin/env python3
"""Local viewer for moqx's metrics dashboard (tools/metrics-dashboard.html in moqx).

Serves the dashboard page AND proxies the relay's /metrics from the same
origin, so the browser's CORS check never comes into play (the moqx admin
server sends no Access-Control-Allow-Origin).

  MOQX_DASHBOARD=<path to metrics-dashboard.html> python3 dash.py relay_host [relay_admin_port] [listen_port]
  then open http://localhost:8080/  and press Start.
"""
import http.server
import os
import socketserver
import sys
import urllib.request
from pathlib import Path

RELAY = sys.argv[1] if len(sys.argv) > 1 else sys.exit("usage: dash.py relay_host [relay_admin_port] [listen_port]")
APORT = sys.argv[2] if len(sys.argv) > 2 else "4500"
LPORT = int(sys.argv[3]) if len(sys.argv) > 3 else 8080
PAGE = Path(os.environ.get("MOQX_DASHBOARD", "metrics-dashboard.html"))


class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/metrics"):
            try:
                with urllib.request.urlopen(
                        f"http://{RELAY}:{APORT}/metrics?omit_metadata=1", timeout=3) as r:
                    body = r.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
            except Exception as e:
                body = f"# relay unreachable: {e}\n".encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
        elif self.path in ("/", "/index.html"):
            body = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


socketserver.ThreadingTCPServer.allow_reuse_address = True
socketserver.ThreadingTCPServer.daemon_threads = True
print(f"dashboard  http://localhost:{LPORT}/")
print(f"  metrics field -> http://localhost:{LPORT}/metrics "
      f"(proxying {RELAY}:{APORT})")
# Bind every interface, not loopback: under WSL the browser runs on Windows and
# reaches this over the WSL adapter's address, which a 127.0.0.1 bind refuses.
# Threaded so a slow /metrics fetch cannot stall the page.
with socketserver.ThreadingTCPServer(("0.0.0.0", LPORT), H) as httpd:
    httpd.serve_forever()
