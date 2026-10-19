"""Internal HTTP server behind https://censor.local (the addon routes that host
here). Serves /health, the injected script (video control and image placeholders)
and the video job API.

  GET  /health
  GET  /inject.js
  POST /video/jobs            {"src": url, "page": url}  (text/plain, so no CORS preflight)
  GET  /video/<id>/status?active=0|1  active=0: paused or in a background tab
  POST /video/<id>/cancel     the page no longer wants it (closed, navigated)
  GET  /video/<id>/stream     fragmented MP4; streamed while the job runs,
                              Range-capable once it is done
"""

from __future__ import annotations

import json
import logging
import re
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from .config import Config
from .video import Job, VideoError, VideoJobs

log = logging.getLogger("censor_proxy.server")

STATIC = Path(__file__).with_name("static")
JOB_PATH = re.compile(r"^/video/([0-9a-f]{24})/(status|stream)$")
CANCEL_PATH = re.compile(r"^/video/([0-9a-f]{24})/cancel$")
CHUNK = 256 * 1024


class InternalServer:
    def __init__(self, cfg: Config, jobs: VideoJobs, health: Callable[[], dict]):
        self.cfg = cfg
        self.jobs = jobs
        self.health = health
        self.video_hosts = [re.compile(p) for p in cfg.video.hosts]
        handler = type("Handler", (_Handler,), {"app": self})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", cfg.proxy.internal_port), handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="censor-server",
                                       daemon=True)

    def start(self) -> None:
        self.thread.start()

    def reconfigure(self, cfg: Config) -> None:
        """Use a new config; the port is fixed at startup."""
        self.video_hosts = [re.compile(p) for p in cfg.video.hosts]
        self.cfg = cfg

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def inject_js(self) -> bytes:
        js = (STATIC / "inject.js").read_text()
        values = {"__CENSOR_ORIGIN__": f"https://{self.cfg.proxy.health_host}",
                  "__BRAND_NAME__": self.cfg.video.brand_name,
                  "__DEBUG__": "true" if self.cfg.video.debug else "false",
                  "__MAX_HEIGHT__": str(self.cfg.video.max_height),
                  "__VIDEO__": "true" if self.cfg.video.enabled else "false",
                  "__PLACEHOLDERS__": "true" if self.cfg.images.placeholders else "false",
                  "__PLACEHOLDER_MS__": str(self.cfg.images.placeholder_delay_ms),
                  "__PLACEHOLDER_MIN_PX__": str(self.cfg.images.placeholder_min_px),
                  "__PLACEHOLDER_RESERVE_PX__": str(self.cfg.images.placeholder_reserve_px)}
        for key, value in values.items():
            js = js.replace(key, json.dumps(value)[1:-1])
        return js.encode()


class _Handler(BaseHTTPRequestHandler):
    app: InternalServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet; mitmproxy already logs requests
        log.debug(fmt, *args)

    # -- helpers ----------------------------------------------------------

    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Cache-Control", "no-store")

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- routes -----------------------------------------------------------

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Range")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            return self._json(200, self.app.health())
        if path == "/inject.js":
            data = self.app.inject_js()
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        m = JOB_PATH.match(path)
        job = self.app.jobs.get(m.group(1)) if m else None
        if job is None:
            return self._json(404, {"error": "not found"})
        if m.group(2) == "status":
            # Someone is still there; without active=0 they want it now.
            query = parse_qs(urlsplit(self.path).query)
            job.touch(active=query.get("active", ["1"])[0] != "0")
            return self._json(200, job.status(self.app.cfg.video.ready_seconds))
        job.touch()
        self._stream(job)

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/debug" and self.app.cfg.video.debug:
            length = int(self.headers.get("Content-Length") or 0)
            log.warning("inject.js: %s", self.rfile.read(min(length, 8192)).decode(errors="replace"))
            self.send_response(HTTPStatus.NO_CONTENT)
            self._cors()
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        cancel = CANCEL_PATH.match(path)
        if path != "/video/jobs" and not cancel:
            return self._json(404, {"error": "not found"})
        # Only pages on the video allowlist may start or cancel jobs.
        origin = urlsplit(self.headers.get("Origin") or "").hostname or ""
        if not any(p.search(origin) for p in self.app.video_hosts):
            return self._json(403, {"error": "origin not allowed"})
        if cancel:
            self.app.jobs.cancel(cancel.group(1), "cancelled: the page closed or moved on")
            return self._json(200, {"ok": True})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(length, 65536)) or b"{}")
            job = self.app.jobs.start(str(body.get("src", "")), body.get("page"),
                                      self.headers.get("User-Agent"))
        except (ValueError, VideoError) as e:
            return self._json(400, {"error": str(e)})
        status = job.status(self.app.cfg.video.ready_seconds)
        self._json(200, {**status, "status_url": f"/video/{job.id}/status",
                         "stream_url": f"/video/{job.id}/stream"})

    # -- streaming --------------------------------------------------------

    def _stream(self, job: Job) -> None:
        if job.state == "done" and job.path and job.path.exists():
            return self._serve_file(job.path)
        if job.state == "error" or not job.path:
            return self._json(409, {"error": job.error or "job failed"})
        # Still running: send the growing file with chunked encoding until the
        # job finishes. Seeking needs the finished file (Range support).
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        sent = 0
        try:
            while True:
                with job.changed:
                    if job.state == "running" and not _grew(job.path, sent):
                        job.changed.wait(timeout=1.0)
                    finished = job.state in ("done", "error")
                sent = self._send_new(job.path, sent)
                job.touch()
                if finished:
                    sent = self._send_new(job.path, sent)
                    break
            self.wfile.write(b"0\r\n\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_new(self, path: Path, offset: int) -> int:
        if not path.exists():
            return offset
        with path.open("rb") as f:
            f.seek(offset)
            while chunk := f.read(CHUNK):
                self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                offset += len(chunk)
        return offset

    def _serve_file(self, path: Path) -> None:
        size = path.stat().st_size
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        try:
            with path.open("rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0 and (chunk := f.read(min(CHUNK, remaining))):
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass


def _grew(path: Path, sent: int) -> bool:
    return path.exists() and path.stat().st_size > sent
