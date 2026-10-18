"""mitmproxy addon: censor female explicit regions in image responses.

Run with:  mitmdump -s censor.py
"""

from __future__ import annotations

import asyncio
import json
import logging
from concurrent.futures import ThreadPoolExecutor

from mitmproxy import ctx, http

from censor_proxy.config import Config, load_config
from censor_proxy.detection_log import DetectionLog
from censor_proxy.imaging import Censor, ScanResult

log = logging.getLogger("censor_proxy")


class CensorAddon:
    def __init__(self):
        self.cfg: Config | None = None
        self.censor: Censor | None = None
        self.detections: DetectionLog | None = None
        self.pool: ThreadPoolExecutor | None = None
        self.in_flight = 0

    def running(self):
        self.cfg = load_config()
        self.censor = Censor(self.cfg)
        self.detections = DetectionLog(self.cfg)
        self.pool = ThreadPoolExecutor(max_workers=self.cfg.model.workers,
                                       thread_name_prefix="censor")
        if self.cfg.proxy.passthrough_hosts:
            ctx.options.update(ignore_hosts=list(self.cfg.proxy.passthrough_hosts))
        log.info("censor proxy ready: model=%s style=%s", self.cfg.model.path or "320n (bundled)",
                 self.cfg.censor.style)

    def done(self):
        if self.pool:
            self.pool.shutdown(wait=False, cancel_futures=True)

    def request(self, flow: http.HTTPFlow) -> None:
        if self.cfg and flow.request.pretty_host == self.cfg.proxy.health_host:
            body = {"ready": self.censor is not None, "in_flight": self.in_flight}
            if flow.request.path.startswith("/health"):
                flow.response = http.Response.make(
                    200 if self.censor else 503, json.dumps(body),
                    {"Content-Type": "application/json", "Cache-Control": "no-store"})
            else:
                flow.response = http.Response.make(404)

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        # Runs after mitmproxy's StreamBodies addon. Buffer image bodies we
        # intend to scan even if they exceed stream_large_bodies.
        if self.cfg is None or not _is_image(flow):
            return
        length = flow.response.headers.get("Content-Length", "")
        if length.isdigit() and int(length) > self.cfg.images.max_bytes:
            flow.response.stream = True
        else:
            flow.response.stream = False

    async def response(self, flow: http.HTTPFlow) -> None:
        if self.censor is None or not _is_image(flow) or flow.response.stream:
            return
        data = flow.response.content
        if not data:
            return
        mime = flow.response.headers.get("Content-Type", "").split(";")[0].strip().lower()
        loop = asyncio.get_running_loop()
        self.in_flight += 1
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(self.pool, self.censor.censor_bytes, data, mime),
                timeout=self.cfg.censor.timeout_seconds)
        except Exception as e:
            reason = "timeout" if isinstance(e, TimeoutError) else f"error: {type(e).__name__}: {e}"
            log.warning("detection failed for %s: %s", flow.request.pretty_host, reason)
            if self.cfg.censor.fail_closed:
                result = await loop.run_in_executor(None, self.censor.fail_closed, data, reason)
            else:
                result = ScanResult(data, "error", f"{reason}; fail_closed off, delivered unmodified")
        finally:
            self.in_flight -= 1

        if result.changed:
            flow.response.content = result.data
            # Integrity headers describe the original body. ETag and
            # Last-Modified stay so the browser can keep caching the result.
            for header in ("Content-MD5", "Digest", "Repr-Digest", "Content-Digest"):
                flow.response.headers.pop(header, None)
        self.detections.record(result, flow.request.pretty_host, flow.request.pretty_url, mime)


def _is_image(flow: http.HTTPFlow) -> bool:
    # Partial (206) and error responses can't be decoded as whole images.
    if flow.response is None or flow.response.status_code != 200:
        return False
    ctype = flow.response.headers.get("Content-Type", "").lower()
    return ctype.startswith("image/") and not ctype.startswith("image/svg")


addons = [CensorAddon()]
