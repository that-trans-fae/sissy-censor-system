"""The addon's hooks driven directly with mitmproxy test flows, for behaviour
the real-proxy tests can't set up (a detection pool that is busy)."""

import asyncio
import base64
import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest
from conftest import FakeDetector
from mitmproxy.test import tflow
from PIL import Image

import censor as addon_module
from censor_proxy.detection_log import DetectionLog
from censor_proxy.imaging import Censor


def png(w, h):
    rng = np.random.default_rng(0)
    buf = io.BytesIO()
    Image.fromarray(rng.integers(0, 256, (h, w, 3), dtype=np.uint8)).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def busy(make_config):
    """An addon whose only detection worker is stuck on a slow image."""
    cfg = make_config("logging: {level: 'off'}\nmodel: {resources: custom, workers: 1}\n")
    addon = addon_module.CensorAddon()
    addon.cfg = cfg
    addon.censor = Censor(cfg, FakeDetector())
    addon.detections = DetectionLog(cfg)
    addon.pool = ThreadPoolExecutor(max_workers=1)
    release = threading.Event()
    addon.pool.submit(release.wait)
    yield addon
    release.set()
    addon.pool.shutdown()


def flow(body, ctype):
    f = tflow.tflow(resp=True)
    f.response.headers["Content-Type"] = ctype
    f.response.content = body
    return f


def run(coro, timeout):
    return asyncio.run(asyncio.wait_for(coro, timeout))


def test_full_image_waits_for_a_worker(busy):
    with pytest.raises(TimeoutError):
        run(busy._censor_image(flow(png(200, 160), "image/png"), "image/png"), 1)


def test_small_image_skips_the_queue(busy):
    run(busy._censor_image(flow(png(8, 8), "image/png"), "image/png"), 5)


def test_body_with_only_tiny_data_uris_skips_the_queue(busy):
    body = b'<img src="data:image/png;base64,' + base64.b64encode(png(8, 8)) + b'">'
    f = flow(body, "text/html")
    run(busy._censor_text(f), 5)
    assert f.response.content == body


def gif(frames=6):
    rng = np.random.default_rng(1)
    ims = [Image.fromarray(rng.integers(0, 256, (160, 200, 3), dtype=np.uint8)) for _ in range(frames)]
    buf = io.BytesIO()
    ims[0].save(buf, "GIF", save_all=True, append_images=ims[1:], duration=50)
    return buf.getvalue()


def addon_with(cfg, detector, workers):
    addon = addon_module.CensorAddon()
    addon.cfg = cfg
    addon.censor = Censor(cfg, detector)
    addon.detections = DetectionLog(cfg)
    addon.pool = ThreadPoolExecutor(max_workers=workers)
    addon.animation_slots = addon_module._animation_slots(cfg)
    return addon


def test_timed_out_scan_frees_its_worker(make_config):
    # Before the deadline, a timed-out GIF kept the only worker and the
    # next image waited for the whole animation.
    cfg = make_config("logging: {level: 'off'}\nmodel: {resources: custom, workers: 1}\n"
                      "censor: {timeout_seconds: 0.2}\nimages: {frame_step: 1}\n")
    addon = addon_with(cfg, FakeDetector(lambda bgr: time.sleep(0.1) or []), 1)

    async def scenario():
        slow = flow(gif(40), "image/gif")  # 40 frames x 0.1 s if it ran on
        await addon._censor_image(slow, "image/gif")
        start = time.perf_counter()
        await addon._censor_image(flow(png(200, 160), "image/png"), "image/png")
        return time.perf_counter() - start
    try:
        assert run(scenario(), 10) < 1.5
    finally:
        addon.pool.shutdown()


def test_still_images_pass_animations(make_config):
    # Two workers, one kept for stills: with an animation holding its slot
    # and another waiting for it, a still image still reaches the model.
    cfg = make_config("logging: {level: 'off'}\nmodel: {resources: custom, workers: 2}\n"
                      "images: {still_workers: 1}\n")
    release = threading.Event()
    seen = []

    def detect(bgr):
        seen.append(bgr.shape[:2])
        release.wait(10)
        return []
    addon = addon_with(cfg, FakeDetector(detect), 2)

    async def scenario():
        gifs = [asyncio.create_task(addon._censor_image(flow(gif(), "image/gif"), "image/gif"))
                for _ in range(2)]
        await asyncio.sleep(0.3)
        still = asyncio.create_task(addon._censor_image(flow(png(240, 180), "image/png"), "image/png"))
        await asyncio.sleep(0.5)
        reached = (180, 240) in seen
        release.set()
        await asyncio.gather(still, *gifs)
        return reached
    try:
        assert run(scenario(), 30)
    finally:
        release.set()
        addon.pool.shutdown()
