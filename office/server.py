"""Live mode: a local server that keeps the page up to date without reloading it."""

import http.server
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .build import Options, build, output_files, sources_signature, version_of
from .config import DEFAULT_LANGUAGE, THRESHOLDS, program_signature
from .usage import current_account, refresh_usage


class LiveState:
    def __init__(self, version: str):
        self.version = version
        self.usage_requested = threading.Event()


def _handler(files: dict[str, Path], live: LiveState):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (name required by BaseHTTPRequestHandler)
            url = urlparse(self.path)
            if url.path == "/usage":
                live.usage_requested.set()
                body, kind = b"ok", "text/plain; charset=utf-8"
            elif url.path == "/version":
                body, kind = live.version.encode(), "text/plain; charset=utf-8"
            elif url.path in ("/", "/office.html"):
                language = parse_qs(url.query).get("lang", [DEFAULT_LANGUAGE])[0]
                body, kind = files.get(language, files[DEFAULT_LANGUAGE]).read_bytes(), "text/html; charset=utf-8"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


def _refresh_usage_forever(live: LiveState) -> None:
    """Every few minutes, when the account changes or when the meters are clicked."""
    account, next_time = None, 0.0
    while True:
        now_account = current_account()
        if live.usage_requested.is_set() or now_account != account or time.time() >= next_time:
            live.usage_requested.clear()
            refresh_usage()
            account = now_account
            next_time = time.time() + max(1, THRESHOLDS["usage_refresh_minutes"]) * 60
        live.usage_requested.wait(10)


def serve(options: Options, port: int, on_ready) -> None:
    signature = sources_signature(options)
    live = LiveState(version_of(signature))
    build(options, live.version)
    program = program_signature()
    http.server.ThreadingHTTPServer.allow_reuse_address = True
    try:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), _handler(output_files(options.output), live))
    except OSError:
        sys.exit(f"Port {port} is busy. Use another one, for example --port {port + 1}.")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=_refresh_usage_forever, args=(live,), daemon=True).start()
    on_ready(f"http://localhost:{port}/")
    try:
        while True:
            time.sleep(options.watch)
            if program_signature() != program:
                print(time.strftime("%H:%M:%S"), "program changed: restarting")
                server.shutdown()
                server.server_close()
                arguments = sys.argv if "--no-open" in sys.argv else [*sys.argv, "--no-open"]
                os.execv(sys.executable, [sys.executable, *arguments])
            current = sources_signature(options)
            if current != signature:
                signature = current
                live.version = version_of(signature)
                build(options, live.version)
                print(time.strftime("%H:%M:%S"), "updated")
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
