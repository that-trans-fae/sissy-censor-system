"""Run the real addon in mitmdump against a local server, to check what the
proxy actually buffers and scans (unit tests call censor_bytes directly and
can't see mitmproxy's streaming decisions)."""

import io
import json
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

SRC = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not shutil.which("mitmdump"), reason="mitmdump not installed")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def site(tmp_path):
    root = tmp_path / "site"
    root.mkdir()
    # ~6 MB of noise as PNG: bigger than mitmproxy's usual streaming cut-offs.
    rng = np.random.default_rng(0)
    Image.fromarray(rng.integers(0, 256, (1500, 1400, 3), dtype=np.uint8)).save(root / "big.png")
    (root / "page.html").write_text("<!doctype html><html><head><title>t</title></head><body></body></html>")
    (root / "blob.bin").write_bytes(b"\0" * 1024)
    (root / "clip.mp4").write_bytes(b"\0\0\0\x18ftypmp42" + b"\0" * 200_000)
    handler = lambda *a, **k: SimpleHTTPRequestHandler(*a, directory=str(root), **k)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def proxy(tmp_path):
    log = tmp_path / "detections.jsonl"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"logging: {{level: debug, path: '{log}'}}\n"
                   f"video: {{cache_dir: '{tmp_path / 'video'}'}}\n"
                   f"proxy: {{internal_port: {free_port()}}}\n")
    port = free_port()
    proc = subprocess.Popen(
        ["mitmdump", "-s", str(SRC / "censor.py"), "--listen-host", "127.0.0.1",
         "--listen-port", str(port), "--set", f"confdir={tmp_path / 'mitm'}"],
        cwd=SRC, env={"CENSOR_CONFIG": str(cfg), "PATH": "/usr/local/bin:/usr/bin:/bin",
                      "HOME": str(tmp_path), "PYTHONUNBUFFERED": "1"},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out, ready = [], threading.Event()

    def drain():
        for line in proc.stdout:
            out.append(line)
            if "censor proxy ready" in line:
                ready.set()
    threading.Thread(target=drain, daemon=True).start()
    if not ready.wait(timeout=60):
        proc.kill()
        pytest.fail("proxy did not start:\n" + "".join(out))
    yield urllib.request.build_opener(urllib.request.ProxyHandler({"http": f"http://127.0.0.1:{port}"})), log
    proc.terminate()
    proc.wait(timeout=10)


def entries(log):
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


def test_large_image_is_scanned_not_streamed(site, proxy):
    root, base = site
    opener, log = proxy
    assert (root / "big.png").stat().st_size > 5_000_000
    body = opener.open(base + "/big.png", timeout=60).read()
    assert body == (root / "big.png").read_bytes()  # noise: nothing to censor
    scanned = [e for e in entries(log) if e.get("url", "").endswith("/big.png")]
    assert scanned and scanned[0]["action"] == "clean"


def test_html_page_gets_script_and_other_bodies_pass(site, proxy):
    root, base = site
    opener, _ = proxy
    req = urllib.request.Request(base + "/page.html", headers={"Sec-Fetch-Dest": "document"})
    assert b'src="https://censor.local/inject.js"' in opener.open(req, timeout=30).read()
    assert opener.open(base + "/blob.bin", timeout=30).read() == b"\0" * 1024


def test_video_opened_directly_becomes_control_page(site, proxy):
    root, base = site
    opener, _ = proxy
    req = urllib.request.Request(base + "/clip.mp4", headers={"Sec-Fetch-Dest": "document"})
    with opener.open(req, timeout=30) as r:
        body = r.read()
        assert r.headers["Content-Type"].startswith("text/html")
    assert b'src="https://censor.local/inject.js"' in body
    assert f'<video src="{base}/clip.mp4" preload="none"'.encode() in body
    # The same URL loaded by a <video> element passes through untouched.
    req = urllib.request.Request(base + "/clip.mp4", headers={"Sec-Fetch-Dest": "video"})
    assert opener.open(req, timeout=30).read() == (root / "clip.mp4").read_bytes()


def test_strip_accept():
    import sys
    sys.path.insert(0, str(SRC))
    from censor import strip_accept
    chrome = "image/jxl,image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
    assert strip_accept(chrome, frozenset({"image/jxl"})) == \
        "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
    assert strip_accept("image/jxl", frozenset({"image/jxl"})) == "*/*"
    assert strip_accept("text/html", frozenset({"image/jxl"})) == "text/html"
