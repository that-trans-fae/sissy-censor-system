"""mitmproxy addon: censor female explicit regions in image responses and in
base64 data-URI images embedded in text bodies, and inject the video control
into pages on allowlisted hosts.

Run with:  mitmdump -s censor.py
"""

from __future__ import annotations

import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from mitmproxy import ctx, http

from censor_proxy import datauri, inject
from censor_proxy.config import Config, load_config
from censor_proxy.detection_log import DetectionLog
from censor_proxy.imaging import Censor, ScanResult
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
        self.video_hosts: list[re.Pattern] = []
        self.in_flight = 0

    def running(self):
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
        log.info("censor proxy ready: model=%s style=%s", self.cfg.model.path or "320n (bundled)",
                 self.cfg.censor.style)

    def done(self):
        if self.pool:
            self.pool.shutdown(wait=False, cancel_futures=True)
        if self.server:
            self.server.stop()

    def _health(self) -> dict:
        return {"ready": self.censor is not None, "in_flight": self.in_flight,
                "video_jobs": self.jobs.active() if self.jobs else 0}

    def request(self, flow: http.HTTPFlow) -> None:
        # The proxy's own host is served by the internal server.
        if self.cfg and flow.request.pretty_host == self.cfg.proxy.health_host:
            flow.metadata["censor_internal"] = True
            flow.request.scheme = "http"
            flow.request.host = "127.0.0.1"
            flow.request.port = self.cfg.proxy.internal_port

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        """Buffer only the bodies the addon inspects; stream everything else.

        Streaming is decided here rather than with mitmproxy's
        stream_large_bodies, which is applied after this hook and would
        stream large images past the censor."""
        if self.cfg is None:
            return
        if flow.metadata.get("censor_internal"):
            flow.response.stream = True  # video streams must not be buffered
        elif _is_image(flow):
            # Always buffered: oversized images fail closed in censor_bytes.
            flow.response.stream = False
        elif self._is_data_uri_text(flow) or self._is_video_page(flow):
            length = flow.response.headers.get("Content-Length", "")
            flow.response.stream = (length.isdigit()
                                    and int(length) > self.cfg.data_uris.max_body_bytes)
        else:
            flow.response.stream = True

    async def response(self, flow: http.HTTPFlow) -> None:
        if self.censor is None or flow.response is None or flow.response.stream:
            return
        if _is_image(flow):
            await self._censor_image(flow)
            return
        if self._is_video_page(flow):
            self._inject(flow)
        if self._is_data_uri_text(flow):
            await self._censor_text(flow)

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

    async def _censor_image(self, flow: http.HTTPFlow) -> None:
        data = flow.response.content
        if not data:
            return
        mime = _mime(flow)
        loop = asyncio.get_running_loop()
        self.in_flight += 1
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(self.pool, self.censor.censor_bytes, data, mime),
                timeout=self.cfg.censor.timeout_seconds)
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
        self.detections.record(result, flow.request.pretty_host, flow.request.pretty_url, mime)

    async def _censor_text(self, flow: http.HTTPFlow) -> None:
        body = flow.response.content
        matches = datauri.find(body) if body else []
        if not matches:
            return
        fail_closed = self.cfg.censor.fail_closed
        loop = asyncio.get_running_loop()
        self.in_flight += 1
        try:
            # Each image gets the per-image budget; one slow image shouldn't
            # cost the rest of the page.
            result = await asyncio.wait_for(
                loop.run_in_executor(self.pool, lambda: datauri.censor_data_uris(
                    body, self.censor, fail_closed=fail_closed, matches=matches)),
                timeout=self.cfg.censor.timeout_seconds * len(matches))
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
            self.detections.record(r, flow.request.pretty_host, flow.request.pretty_url, mime,
                                   source="data-uri")

    def _is_data_uri_text(self, flow: http.HTTPFlow) -> bool:
        return (self.cfg.data_uris.enabled and flow.response is not None
                and flow.response.status_code == 200
                and _mime(flow) in self.cfg.data_uris.content_types)


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


def _is_image(flow: http.HTTPFlow) -> bool:
    # Partial (206) and error responses can't be decoded as whole images.
    if flow.response is None or flow.response.status_code != 200:
        return False
    ctype = flow.response.headers.get("Content-Type", "").lower()
    return ctype.startswith("image/") and not ctype.startswith("image/svg")


addons = [CensorAddon()]
