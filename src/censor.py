"""mitmproxy addon: censor female explicit regions in image responses
(including ones whose Content-Type doesn't say image) and in base64 data-URI
images embedded in text bodies, and inject the video control into pages on
allowlisted hosts. Whitelisted domains pass untouched; the admin panel
(censor_proxy/admin.py) can change the config while the proxy runs.

Run with:  mitmdump -s censor.py
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from mitmproxy import ctx, http

from censor_proxy import datauri, inject
from censor_proxy.admin import AdminServer, UserConfigFile
from censor_proxy.config import Config, config_path, load_config
from censor_proxy.detection_log import DetectionLog
from censor_proxy.imaging import Censor, ScanResult, sniff
from censor_proxy.server import InternalServer
from censor_proxy.video import VideoJobs

log = logging.getLogger("censor_proxy")


class CensorAddon:
    def __init__(self):
        self.cfg: Config | None = None
        self.censor: Censor | None = None
        self.detections: DetectionLog | None = None
        self.pool: ThreadPoolExecutor | None = None
        self.jobs: VideoJobs | None = None
        self.server: InternalServer | None = None
        self.admin: AdminServer | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.video_hosts: list[re.Pattern] = []
        self.in_flight = 0
        self.started = time.time()
        # Every key present from the start: the admin thread copies this.
        self.stats = Counter(dict.fromkeys(
            ("censored", "clean", "skipped", "fail_closed", "error", "unscanned", "whitelisted"), 0))

    def running(self):
        self.loop = asyncio.get_running_loop()
        self.cfg = load_config()
        self.censor = Censor(self.cfg)
        self.detections = DetectionLog(self.cfg)
        self.pool = ThreadPoolExecutor(max_workers=self.cfg.model.workers,
                                       thread_name_prefix="censor")
        self.jobs = VideoJobs(self.cfg)
        self.server = InternalServer(self.cfg, self.jobs, self._health)
        self.server.start()
        self.video_hosts = [re.compile(p) for p in self.cfg.video.hosts]
        # Lazy upstream connections, so CONNECT to the internal host doesn't
        # fail trying to reach a real censor.local.
        ctx.options.update(connection_strategy="lazy")
        if self.cfg.proxy.passthrough_hosts:
            ctx.options.update(ignore_hosts=list(self.cfg.proxy.passthrough_hosts))
        if self.cfg.admin.enabled:
            self.admin = AdminServer(self.cfg, self, UserConfigFile(config_path()))
            self.admin.start()
        log.info("censor proxy ready: model=%s style=%s", self.cfg.model.path or "320n (bundled)",
                 self.cfg.censor.style)

    def done(self):
        if self.pool:
            self.pool.shutdown(wait=False, cancel_futures=True)
        if self.server:
            self.server.stop()
        if self.admin:
            self.admin.stop()

    def _health(self) -> dict:
        return {"ready": self.censor is not None, "in_flight": self.in_flight,
                "video_jobs": self.jobs.active() if self.jobs else 0}

    # -- admin panel ------------------------------------------------------

    def status(self) -> dict:
        cfg = self.cfg
        return {
            "ready": self.censor is not None,
            "uptime_seconds": round(time.time() - self.started),
            "in_flight": self.in_flight,
            "counts": dict(self.stats),
            "model": cfg.model.name_for(cfg.model.path, cfg.model.resolution),
            "preset": cfg.detection.preset,
            "resolution": cfg.model.resolution,
            "tiling": cfg.model.tiling,
            "cache_entries": self.censor.cached() if self.censor else 0,
            "censor_style": cfg.censor.style,
            "fail_closed": cfg.censor.fail_closed,
            "video": {"enabled": cfg.video.enabled,
                      "model": cfg.model.name_for(cfg.video.model_path, cfg.video.model_resolution),
                      "active_jobs": self.jobs.active() if self.jobs else 0},
            "log_level": cfg.logging.level,
            "log_path": cfg.logging.path,
            "whitelist": list(cfg.whitelist.domains),
            "brand_name": cfg.video.brand_name,
            "health_host": cfg.proxy.health_host,
            "admin_host": cfg.proxy.admin_host,
        }

    def apply_config(self, cfg: Config) -> None:
        """Switch to a new config while running. Called from the admin
        server's thread: anything slow (loading a model) happens here, and
        the switch itself on the event loop, between requests."""
        old = self.cfg
        censor = self.censor
        if (cfg.model, cfg.detection, cfg.censor, cfg.images, cfg.cache) != \
                (old.model, old.detection, old.censor, old.images, old.cache):
            # Cached results depend on the thresholds, so a fresh cache.
            censor = Censor(cfg, self.censor.detector if cfg.model == old.model else None)
        pool = None
        if cfg.model.workers != old.model.workers:
            pool = ThreadPoolExecutor(max_workers=cfg.model.workers, thread_name_prefix="censor")

        async def switch():
            if pool:
                self.pool, previous = pool, self.pool
                previous.shutdown(wait=False)  # queued scans still finish
            self.cfg, self.censor = cfg, censor
            self.detections = DetectionLog(cfg)
            self.video_hosts = [re.compile(p) for p in cfg.video.hosts]
            self.jobs.reconfigure(cfg)
            self.server.reconfigure(cfg)
            if cfg.proxy.passthrough_hosts != old.proxy.passthrough_hosts:
                ctx.options.update(ignore_hosts=list(cfg.proxy.passthrough_hosts))
        asyncio.run_coroutine_threadsafe(switch(), self.loop).result(timeout=30)
        log.info("config applied: model=%s style=%s whitelist=%s", cfg.model.path or "320n (bundled)",
                 cfg.censor.style, ", ".join(cfg.whitelist.domains) or "none")

    def _whitelisted(self, flow: http.HTTPFlow) -> str | None:
        """The whitelisted domain covering this request or the page that made it."""
        wl = self.cfg.whitelist
        if not wl.domains:
            return None
        if domain := wl.match(flow.request.pretty_host):
            return domain
        if wl.match_referer:
            for header in ("Referer", "Origin"):
                if (value := flow.request.headers.get(header)) and \
                        (domain := wl.match(urlsplit(value).hostname or "")):
                    return domain
        return None

    # -- hooks ------------------------------------------------------------

    def request(self, flow: http.HTTPFlow) -> None:
        if self.cfg is None:
            return
        host = flow.request.pretty_host
        # The proxy's own host is served by the internal server.
        if host == self.cfg.proxy.health_host:
            flow.metadata["censor_internal"] = True
            flow.request.scheme = "http"
            flow.request.host = "127.0.0.1"
            flow.request.port = self.cfg.proxy.internal_port
            return
        # The admin host goes to the admin listener, keeping its Host so the
        # panel knows its origin (https://scs.local).
        if host == self.cfg.proxy.admin_host and self.admin:
            flow.metadata["censor_internal"] = True
            flow.request.scheme = "http"
            flow.request.host = "127.0.0.1"
            flow.request.port = self.admin.port
            flow.request.host_header = host
            return
        if domain := self._whitelisted(flow):
            flow.metadata["censor_whitelisted"] = domain
            return
        accept = flow.request.headers.get("Accept")
        if accept and self.cfg.images.strip_accept:
            stripped = strip_accept(accept, self.cfg.images.strip_accept)
            if stripped != accept:
                flow.request.headers["Accept"] = stripped

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        """Buffer only the bodies the addon inspects; stream everything else.

        Streaming is decided here rather than with mitmproxy's
        stream_large_bodies, which is applied after this hook and would
        stream large images past the censor."""
        if self.cfg is None:
            return
        if flow.metadata.get("censor_internal"):
            flow.response.stream = True  # video streams must not be buffered
        elif domain := flow.metadata.get("censor_whitelisted"):
            flow.response.stream = True
            self.stats["whitelisted"] += 1
            if _requested_as_image(flow):
                self._log_unscanned(flow, f"whitelisted ({domain})", count=False)
            return
        elif self._is_video_document(flow):
            self._replace_with_video_page(flow)
        elif _is_image(flow):
            # Always buffered: oversized images fail closed in censor_bytes.
            flow.response.stream = False
        elif self._is_sniff_candidate(flow):
            flow.metadata["censor_sniff"] = True
            flow.response.stream = False
        elif self._is_data_uri_text(flow) or self._is_video_page(flow):
            length = flow.response.headers.get("Content-Length", "")
            flow.response.stream = (length.isdigit()
                                    and int(length) > self.cfg.data_uris.max_body_bytes)
        else:
            flow.response.stream = True
        if flow.response.stream and _requested_as_image(flow):
            self._log_unscanned(flow, f"streamed (status {flow.response.status_code})")

    async def response(self, flow: http.HTTPFlow) -> None:
        if self.censor is None or flow.response is None or flow.response.stream:
            return
        if _is_image(flow):
            await self._censor_image(flow, _mime(flow))
            return
        if flow.metadata.get("censor_sniff"):
            if mime := sniff(flow.response.content or b""):
                await self._censor_image(flow, mime, source="sniffed")
                return
        if self._is_video_page(flow):
            self._inject(flow)
        if self._is_data_uri_text(flow):
            await self._censor_text(flow)
        if _requested_as_image(flow) and not flow.metadata.get("censor_logged"):
            ctype = flow.response.headers.get("Content-Type", "") or "missing"
            self._log_unscanned(flow, f"no supported image found (Content-Type {ctype})")

    def _is_sniff_candidate(self, flow: http.HTTPFlow) -> bool:
        """A 200 response that may be an image despite its Content-Type."""
        icfg = self.cfg.images
        if flow.response.status_code != 200:
            return False
        mime = _mime(flow)
        if icfg.sniff_image_requests and _requested_as_image(flow) and mime != "image/svg+xml":
            return True
        length = flow.response.headers.get("Content-Length", "")
        return (mime in icfg.sniff_types and length.isdigit()
                and int(length) <= icfg.sniff_max_bytes)

    def _log_unscanned(self, flow: http.HTTPFlow, reason: str, count: bool = True) -> None:
        self._record(flow, ScanResult(b"", "unscanned", reason), _mime(flow), count=count)

    def _record(self, flow: http.HTTPFlow, result: ScanResult, mime: str,
                source: str = "response", count: bool = True) -> None:
        flow.metadata["censor_logged"] = True
        if count:
            self.stats[result.action] += 1
        self.detections.record(result, flow.request.pretty_host, flow.request.pretty_url, mime,
                               source=source)

    def _is_video_document(self, flow: http.HTTPFlow) -> bool:
        """A video opened directly as a page or iframe."""
        dest = flow.request.headers.get("Sec-Fetch-Dest", "")
        return (self.cfg.video.enabled and dest in ("document", "iframe", "frame")
                and flow.response.status_code in (200, 206)
                and _mime(flow).startswith("video/")
                and any(p.search(flow.request.pretty_host) for p in self.video_hosts))

    def _replace_with_video_page(self, flow: http.HTTPFlow) -> None:
        # Headers go to the client as soon as streaming starts, so the swap
        # happens here: new headers, and a stream function that emits our
        # page once and drops the original body.
        page = inject.video_page(flow.request.pretty_url, f"https://{self.cfg.proxy.health_host}")
        flow.response.status_code = 200
        flow.response.reason = "OK"
        flow.response.headers.clear()
        flow.response.headers["Content-Type"] = "text/html; charset=utf-8"
        flow.response.headers["Content-Length"] = str(len(page))
        flow.response.headers["Cache-Control"] = "no-store"
        sent = False

        def emit(chunk: bytes) -> bytes:
            nonlocal sent
            if sent:
                return b""
            sent = True
            return page
        flow.response.stream = emit
        log.info("replaced directly opened video with control page: %s", flow.request.pretty_host)

    def _is_video_page(self, flow: http.HTTPFlow) -> bool:
        # Browsers say what a request is for; only documents and frames get
        # the script, never HTML fetched by a page's own code.
        dest = flow.request.headers.get("Sec-Fetch-Dest", "document")
        return (self.cfg.video.enabled and flow.response.status_code == 200
                and _mime(flow) == "text/html" and dest in ("document", "iframe", "frame")
                and any(p.search(flow.request.pretty_host) for p in self.video_hosts))

    def _inject(self, flow: http.HTTPFlow) -> None:
        origin = f"https://{self.cfg.proxy.health_host}"
        nonce = inject.new_nonce()
        body = inject.inject_html(flow.response.content or b"",
                                  inject.script_tag(origin, nonce if inject.extend_csp_headers(
                                      flow.response.headers, origin, nonce) else None))
        if body is not None:
            _replace_body(flow, body)

    async def _censor_image(self, flow: http.HTTPFlow, mime: str, source: str = "response") -> None:
        data = flow.response.content
        if not data:
            self._log_unscanned(flow, "empty body")
            return
        loop = asyncio.get_running_loop()
        self.in_flight += 1
        try:
            result = await self._run_timed(self.cfg.censor.timeout_seconds,
                                           self.censor.censor_bytes, data, mime)
        except Exception as e:
            reason = _failure_reason(e)
            log.warning("detection failed for %s: %s", flow.request.pretty_host, reason)
            if self.cfg.censor.fail_closed:
                result = await loop.run_in_executor(None, self.censor.fail_closed, data, reason)
            else:
                result = ScanResult(data, "error", f"{reason}; fail_closed off, delivered unmodified")
        finally:
            self.in_flight -= 1

        if result.changed:
            _replace_body(flow, result.data)
        self._record(flow, result, mime, source)

    async def _censor_text(self, flow: http.HTTPFlow) -> None:
        body = flow.response.content
        loop = asyncio.get_running_loop()
        # Off the event loop: JavaScript bundles can run to megabytes.
        matches = await loop.run_in_executor(None, datauri.find, body) if body else []
        if not matches:
            return
        fail_closed = self.cfg.censor.fail_closed
        self.in_flight += 1
        try:
            # Each image gets the per-image budget; one slow image shouldn't
            # cost the rest of the page.
            result = await self._run_timed(
                self.cfg.censor.timeout_seconds * len(matches),
                lambda: datauri.censor_data_uris(body, self.censor, fail_closed=fail_closed,
                                                 matches=matches))
        except Exception as e:
            reason = _failure_reason(e)
            log.warning("data-URI detection failed for %s: %s", flow.request.pretty_host, reason)
            if not fail_closed:
                return
            result = await loop.run_in_executor(None, lambda: datauri.censor_data_uris(
                body, self.censor, fail_closed=True, matches=matches, force_reason=reason))
        finally:
            self.in_flight -= 1

        if result.changed:
            _replace_body(flow, result.body)
        for mime, r in result.results:
            self._record(flow, r, mime, source="data-uri")

    async def _run_timed(self, timeout: float, fn, *args):
        """Run fn on the worker pool. The timeout starts when a worker picks
        the job up, so images queued behind slow ones (big GIFs) aren't
        failed closed just for waiting."""
        loop = asyncio.get_running_loop()
        started = asyncio.Event()

        def job():
            loop.call_soon_threadsafe(started.set)
            return fn(*args)
        future = loop.run_in_executor(self.pool, job)
        await started.wait()
        return await asyncio.wait_for(future, timeout=timeout)

    def _is_data_uri_text(self, flow: http.HTTPFlow) -> bool:
        return (self.cfg.data_uris.enabled and flow.response is not None
                and flow.response.status_code == 200
                and self.cfg.data_uris.matches(_mime(flow)))


def strip_accept(accept: str, remove: frozenset[str]) -> str:
    """Drop media types the censor can't decode from an Accept header, so a
    server that negotiates image formats picks one it can."""
    kept = [part for part in accept.split(",")
            if part.split(";")[0].strip().lower() not in remove]
    return ",".join(kept).strip() or "*/*"


def _mime(flow: http.HTTPFlow) -> str:
    return flow.response.headers.get("Content-Type", "").split(";")[0].strip().lower()


def _failure_reason(e: Exception) -> str:
    return "timeout" if isinstance(e, TimeoutError) else f"error: {type(e).__name__}: {e}"


def _replace_body(flow: http.HTTPFlow, data: bytes) -> None:
    flow.response.content = data
    # Integrity headers describe the original body. ETag and Last-Modified
    # stay so the browser can keep caching the result.
    for header in ("Content-MD5", "Digest", "Repr-Digest", "Content-Digest"):
        flow.response.headers.pop(header, None)


def _requested_as_image(flow: http.HTTPFlow) -> bool:
    # <img>, CSS images and the like; fetch() and XHR say "empty".
    return flow.request.headers.get("Sec-Fetch-Dest", "") == "image"


def _is_image(flow: http.HTTPFlow) -> bool:
    # Partial (206) and error responses can't be decoded as whole images.
    if flow.response is None or flow.response.status_code != 200:
        return False
    ctype = flow.response.headers.get("Content-Type", "").lower()
    return ctype.startswith("image/") and not ctype.startswith("image/svg")


addons = [CensorAddon()]
