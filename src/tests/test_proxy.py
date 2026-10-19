"""Run the real addon in mitmdump against a local server, to check what the
proxy actually buffers and scans (unit tests call censor_bytes directly and
can't see mitmproxy's streaming decisions)."""

import base64
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
    # Real images behind labels that don't say image, and an SVG and a script
    # carrying a base64 PNG.
    noise = rng.integers(0, 256, (200, 240, 3), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(noise).save(buf, "PNG")
    small = buf.getvalue()
    (root / "image.bin").write_bytes(small)
    (root / "image.txt").write_bytes(small)
    embedded = b"data:image/png;charset=utf-8;base64," + base64.b64encode(small)
    (root / "ad.svg").write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg"><image href="'
                                  + embedded + b'"/></svg>')
    (root / "ad.js").write_bytes(b'var creative = "' + embedded + b'";')
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
                   f"proxy: {{internal_port: {free_port()}}}\n"
                   f"admin: {{bind: 127.0.0.1, port: {free_port()}}}\n"
                   "whitelist: {domains: [youtube.com]}\n")
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


def test_mislabelled_images_are_sniffed_and_scanned(site, proxy):
    root, base = site
    opener, log = proxy
    # application/octet-stream: sniffed on any request.
    assert opener.open(base + "/image.bin", timeout=30).read() == (root / "image.bin").read_bytes()
    # text/plain: sniffed when the browser asked for an image.
    req = urllib.request.Request(base + "/image.txt", headers={"Sec-Fetch-Dest": "image"})
    opener.open(req, timeout=30).read()
    scanned = {e["url"].rsplit("/", 1)[1]: e for e in entries(log)}
    for name in ("image.bin", "image.txt"):
        assert scanned[name]["source"] == "sniffed" and scanned[name]["action"] == "clean"
        assert scanned[name]["mime"] == "image/png"


def test_svg_and_script_data_uris_scanned(site, proxy):
    root, base = site
    opener, log = proxy
    for name in ("ad.svg", "ad.js"):
        opener.open(base + "/" + name, timeout=30).read()
    found = [e["url"].rsplit("/", 1)[1] for e in entries(log) if e.get("source") == "data-uri"]
    assert sorted(found) == ["ad.js", "ad.svg"]


def test_unscanned_image_requests_logged(site, proxy):
    root, base = site
    opener, log = proxy
    for path in ("/missing.png", "/page.html"):
        req = urllib.request.Request(base + path, headers={"Sec-Fetch-Dest": "image"})
        try:
            opener.open(req, timeout=30).read()
        except urllib.error.HTTPError:
            pass
    reasons = {e["url"].rsplit("/", 1)[1]: e["reason"] for e in entries(log)
               if e["action"] == "unscanned"}
    assert reasons == {"missing.png": "streamed (status 404)",
                       "page.html": "no supported image found (Content-Type text/html)"}


def test_strip_accept():
    import sys
    sys.path.insert(0, str(SRC))
    from censor import strip_accept
    chrome = "image/jxl,image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
    assert strip_accept(chrome, frozenset({"image/jxl"})) == \
        "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
    assert strip_accept("image/jxl", frozenset({"image/jxl"})) == "*/*"
    assert strip_accept("text/html", frozenset({"image/jxl"})) == "text/html"


def test_whitelisted_pages_pass_unscanned(site, proxy):
    root, base = site
    opener, log = proxy
    # Requested by a whitelisted page: delivered as is, logged as unscanned.
    req = urllib.request.Request(base + "/image.bin", headers={
        "Sec-Fetch-Dest": "image", "Referer": "https://www.youtube.com/watch?v=x"})
    assert opener.open(req, timeout=30).read() == (root / "image.bin").read_bytes()
    req = urllib.request.Request(base + "/page.html", headers={
        "Sec-Fetch-Dest": "document", "Referer": "https://m.youtube.com/"})
    assert b"inject.js" not in opener.open(req, timeout=30).read()
    found = {e["url"].rsplit("/", 1)[1]: e for e in entries(log)}
    assert found["image.bin"]["action"] == "unscanned"
    assert found["image.bin"]["reason"] == "whitelisted (youtube.com)"


def test_admin_host_routed_to_panel(proxy):
    opener, _ = proxy
    with opener.open("http://scs.local/admin/api/session", timeout=30) as r:
        assert json.loads(r.read()) == {"pin_set": False, "unlocked": True, "retry_after": 0}
    with opener.open("http://scs.local/admin/", timeout=30) as r:
        assert b"admin.js" in r.read()
