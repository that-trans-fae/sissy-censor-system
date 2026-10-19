"""Internal HTTP server behind https://censor.local (the addon routes that host
here). Serves /health, the injected script (video control and image placeholders)
and the video job API.

  GET  /health
  GET  /inject.js
  POST /video/jobs            {"src": url, "page": url}  (text/plain, so no CORS preflight)
  GET  /video/<id>/status?active=0|1  active=0: paused or in a background tab
  POST /video/<id>/cancel     the page no longer wants it (closed, navigated)
  GET  /video/<id>/init/<run> a run's fMP4 init segment
  GET  /video/<id>/segment/<n>
                              segment n, once done; waits up to SEGMENT_WAIT
                              for it, and restarts the job there unless it
                              will get there soon. 202: not yet, ask again.
                              204: past the end. X-Init names the run whose
                              init segment it needs, X-Offset the time its
                              timestamps start from.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from .config import Config
from .video import Job, JobRefused, VideoError, VideoJobs

log = logging.getLogger("censor_proxy.server")

STATIC = Path(__file__).with_name("static")
JOB_PATH = re.compile(r"^/video/([0-9a-f]{24})/(status|init/(\d{1,6})|segment/(\d{1,6}))$")
CANCEL_PATH = re.compile(r"^/video/([0-9a-f]{24})/cancel$")
# How long a segment request waits for the job before answering 202. Under
# the browser's own timeouts, and short enough that a page that went away
# doesn't keep a thread waiting long.
SEGMENT_WAIT = 15.0


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

    def _origin(self) -> str | None:
        """The request's Origin header when its host is on the video
        allowlist, else None."""
        origin = self.headers.get("Origin") or ""
        host = urlsplit(origin).hostname or ""
        return origin if origin and any(p.search(host) for p in self.app.video_hosts) else None

    def _cors(self) -> None:
        """Let an allowlisted page read the response. Only the job endpoints
        send this: /health and /inject.js are never readable cross-origin, so
        a page can't use them to tell the proxy is running."""
        if origin := self._origin():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Private-Network", "true")
        self.send_header("Vary", "Origin")
        self.send_header("Cache-Control", "no-store")

    def _json(self, status: int, body: dict, cors: bool = True) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        if cors:
            self._cors()
        else:
            self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # -- routes -----------------------------------------------------------

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            return self._json(200, self.app.health(), cors=False)
        if path == "/inject.js":
            data = self.app.inject_js()
            self.send_response(200)
            self.send_header("Cache-Control", "no-store")
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
        if m.group(3) is not None:
            return self._init(job, int(m.group(3)))
        self._segment(job, int(m.group(4)))

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path == "/debug" and self.app.cfg.video.debug:
            if self._origin() is None:  # only our script's pages may write to the log
                return self._json(403, {"error": "origin not allowed"})
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
        origin = self._origin()
        if origin is None:
            return self._json(403, {"error": "origin not allowed"})
        if cancel:
            self.app.jobs.cancel(cancel.group(1), "cancelled: the page closed or moved on")
            return self._json(200, {"ok": True})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(length, 65536)) or b"{}")
            page = body.get("page")
            job = self.app.jobs.start(str(body.get("src", "")), origin,
                                      self.headers.get("User-Agent"),
                                      page=str(page)[:2000] if page else None)
        except JobRefused as e:
            return self._json(429, {"error": str(e)})
        except (ValueError, VideoError) as e:
            return self._json(400, {"error": str(e)})
        status = job.status(self.app.cfg.video.ready_seconds)
        self._json(200, {**status, "status_url": f"/video/{job.id}/status"})

    # -- segments ---------------------------------------------------------

    def _init(self, job: Job, run: int) -> None:
        path = job.init_path(run) if job.out_dir and job.state != "error" else None
        if not path or not path.is_file():
            return self._json(404, {"error": "not found"})
        self._send_file(path)

    def _segment(self, job: Job, n: int) -> None:
        v = self.app.cfg.video
        deadline = time.monotonic() + SEGMENT_WAIT
        # Decided under the job's lock, answered after it: the pump needs it.
        with job.changed:
            while True:
                if job.check(n):
                    outcome, (run, first) = "ready", job.run_of[n]
                    break
                wait = math.ceil(v.seek_wait_seconds / job.segment_seconds) if job.segment_seconds else 0
                job.seek(n, wait)
                if job.state == "error":
                    outcome = "error"
                    break
                if job.state == "done" or (job.segments is not None and n >= job.segments):
                    outcome = "end"
                    break
                left = deadline - time.monotonic()
                if left <= 0:
                    outcome = "later"
                    break
                job.changed.wait(min(left, 1.0))
                job.touch()
        if outcome == "error":
            return self._json(409, {"error": job.error or "job failed"})
        if outcome == "end":
            return self._empty(HTTPStatus.NO_CONTENT)
        if outcome == "later":
            return self._json(202, {"state": job.state})
        self._send_file(job.segment_path(n), {
            "X-Init": str(run),
            "X-Offset": f"{first * job.segment_seconds:.6f}",
            "Access-Control-Expose-Headers": "X-Init, X-Offset",
        })

    def _empty(self, status: int) -> None:
        self.send_response(status)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_file(self, path: Path, headers: dict[str, str] | None = None) -> None:
        try:
            data = path.read_bytes()  # segments are a few MB at most
        except OSError:  # evicted or cancelled meanwhile
            return self._json(404, {"error": "not found"})
        self.send_response(200)
        self._cors()
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass
