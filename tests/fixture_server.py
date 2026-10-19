"""Synthetic service for lifecycle tests.

  fixture_server.py PORT [--crash-file F] [--busy-file F]
GET /health -> {"ok":true,"version":<VERSION file content>}; GET /busy -> {"busy":bool}
If --crash-file exists at start, the process exits 1 immediately (simulates a crash loop).
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

args = sys.argv[1:]
port = int(args[0])
opts = dict(zip(args[1::2], args[2::2]))
crash = opts.get("--crash-file")
busy = opts.get("--busy-file")
here = Path.cwd()
if crash and os.path.exists(crash):
    print("fixture: crash file present, exiting 1", flush=True)
    sys.exit(1)
version = (here / "VERSION").read_text().strip() if (here / "VERSION").exists() else "0"
started = time.time()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/health":
            body = {"ok": True, "version": version, "started": started}
        elif self.path == "/busy":
            body = {"busy": bool(busy and os.path.exists(busy))}
        else:
            body = {"hello": "fixture", "token": "should-not-leak-1234567"}
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


print(f"fixture: listening on {port}, version {version}, secret=hunter2hunter2", flush=True)
HTTPServer(("127.0.0.1", port), H).serve_forever()
