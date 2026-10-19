"""The addon's hooks driven directly with mitmproxy test flows, for behaviour
the real-proxy tests can't set up (a detection pool that is busy)."""

import asyncio
import base64
import io
import threading
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
