import io
import json
import shutil
import subprocess
import threading
import urllib.request
from pathlib import Path

import numpy as np
import pytest
from conftest import FakeDetector, det

from censor_proxy.server import InternalServer
from censor_proxy.video import (Job, Probe, VideoError, VideoJobs, _pump, censor_video,
                                check_source, output_size)

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def test_output_size_caps_height_and_keeps_even():
    assert output_size(Probe(0, 1920, 1080, 30, 10, None), 720) == (1280, 720)
    assert output_size(Probe(0, 801, 1423, 30, 10, None), 720) == (404, 720)
    assert output_size(Probe(0, 640, 360, 30, 10, None), 720) == (640, 360)


def run_pump(cfg, n, detections_per_call, step=4, w=64, h=48):
    frames = [np.full((h, w, 3), i * 10 % 256, np.uint8) for i in range(n)]
    noise = np.random.default_rng(0).integers(0, 256, (h, w, 3), dtype=np.uint8)
    frames = [np.where(np.arange(w)[None, :, None] % 2, f, noise) for f in frames]
    src = io.BytesIO(b"".join(f.astype(np.uint8).tobytes() for f in frames))
    dst = io.BytesIO()
    calls = iter(detections_per_call)
    fake = FakeDetector(lambda bgr: next(calls))
    job = Job("t", "x")
    _pump(src, dst, w, h, w * h * 3, 10.0, step, job, cfg, fake)
    out = np.frombuffer(dst.getvalue(), np.uint8).reshape(n, h, w, 3)
    return frames, out, job, fake


def test_pump_holds_boxes_across_unsampled_frames(cfg):
    box = (8, 8, 16, 16)
    # 10 frames, step 4 -> samples at 0, 4, 8; only frame 4 has a detection.
    frames, out, job, fake = run_pump(cfg, 10, [[], [det("FEMALE_BREAST_EXPOSED", box=box)], []])
    assert fake.calls == 3
    changed = [not np.array_equal(f[8:24, 8:24], o[8:24, 8:24]) for f, o in zip(frames, out)]
    # Frames 1-3 (before sample 4) and 4-11 (until the next sample's boxes
    # replace it) are covered; frame 0 and 9 (after sample 8, no boxes) aren't.
    assert changed == [False, True, True, True, True, True, True, True, True, False]
    assert job.frames_censored == 8 and abs(job.seconds_done - 1.0) < 1e-9


def test_pump_fails_closed_on_detector_error(cfg):
    def boom(bgr):
        raise RuntimeError("model exploded")
    w, h = 32, 32
    src = io.BytesIO(np.random.default_rng(1).integers(0, 256, (2, h, w, 3), dtype=np.uint8).tobytes())
    dst = io.BytesIO()
    _pump(src, dst, w, h, w * h * 3, 10.0, 4, Job("t", "x"), cfg, FakeDetector(boom))
    out = np.frombuffer(dst.getvalue(), np.uint8).reshape(2, h, w, 3).astype(int)
    assert np.abs(np.diff(out, axis=2)).mean() < 20  # whole frame blurred


def make_test_video(path: Path, seconds=2, audio=True):
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=320x240:rate=10:duration={seconds}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True)


@needs_ffmpeg
def test_censor_video_end_to_end(cfg, tmp_path):
    src = tmp_path / "in.mp4"
    make_test_video(src)
    job = Job("t", str(src), path=tmp_path / "out.mp4")
    fake = FakeDetector([det("FEMALE_BREAST_EXPOSED", box=(100, 80, 60, 60))])
    censor_video(job, cfg, fake)
    info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                                      "-show_format", str(job.path)], capture_output=True, check=True).stdout)
    kinds = sorted(s["codec_type"] for s in info["streams"])
    assert kinds == ["audio", "video"]
    assert abs(float(info["format"]["duration"]) - 2.0) < 0.3
    # 10 fps sampled at 4/s -> every 2nd frame goes through detection.
    assert job.frames_censored == 20 and fake.calls == 10


@needs_ffmpeg
def test_censor_video_rejects_unreadable_source(cfg, tmp_path):
    job = Job("t", str(tmp_path / "missing.mp4"), path=tmp_path / "out.mp4")
    with pytest.raises(Exception, match="cannot read source"):
        censor_video(job, cfg, FakeDetector())


@pytest.mark.parametrize("src", [
    "file:///etc/passwd", "ftp://example.com/v.mp4", "http://127.0.0.1:8081/health",
    "http://localhost/v.mp4", "http://192.168.1.1/v.mp4", "http://[::1]/v.mp4", "http://10.0.0.5/x.m3u8",
])
def test_jobs_reject_non_public_sources(cfg, src):
    with pytest.raises(VideoError):
        VideoJobs(cfg, FakeDetector()).start(src)


def test_public_source_accepted():
    check_source("https://93.184.215.14/v.mp4")


@pytest.fixture
def server(make_config, tmp_path):
    # Narrowed hosts, to exercise the origin check (the default allows every site).
    cfg = make_config(f"proxy: {{internal_port: 0}}\n"
                      f"video: {{cache_dir: '{tmp_path}', hosts: ['(^|\\.)reddit\\.com$']}}\n")
    jobs = VideoJobs(cfg, FakeDetector())
    srv = InternalServer(cfg, jobs, lambda: {"ready": True})
    srv.start()
    yield srv, jobs, f"http://127.0.0.1:{srv.httpd.server_address[1]}"
    srv.stop()


def get(url, headers=None):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {})) as r:
        return r.status, dict(r.headers), r.read()


def test_server_health_and_inject_js(server):
    srv, _, base = server
    assert json.loads(get(base + "/health")[2]) == {"ready": True}
    status, headers, body = get(base + "/inject.js")
    assert headers["Content-Type"].startswith("text/javascript")
    assert b'"https://censor.local"' in body and b"__CENSOR_ORIGIN__" not in body
    assert b'"Censor"' in body


def test_server_streams_running_job_then_serves_ranges(server, tmp_path):
    srv, jobs, base = server
    job = Job("a" * 24, "https://example.com/v.mp4", path=tmp_path / "v.mp4", state="running")
    jobs._jobs[job.id] = job
    job.path.write_bytes(b"x" * 1000)

    def finish():
        with job.path.open("ab") as f:
            f.write(b"y" * 500)
        job.update(state="done", seconds_done=1)
    threading.Timer(0.3, finish).start()
    status, headers, body = get(f"{base}/video/{job.id}/stream")
    assert headers.get("Transfer-Encoding") == "chunked" and body == b"x" * 1000 + b"y" * 500

    status, headers, body = get(f"{base}/video/{job.id}/stream", {"Range": "bytes=990-1009"})
    assert status == 206 and headers["Content-Range"] == "bytes 990-1009/1500"
    assert body == b"x" * 10 + b"y" * 10


def post_job(base, body, origin="https://www.reddit.com"):
    req = urllib.request.Request(base + "/video/jobs", data=body, method="POST",
                                 headers={"Origin": origin} if origin else {})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    return e.value.code


def test_server_job_status_and_errors(server):
    srv, jobs, base = server
    assert post_job(base, b'{"src": "ftp://x"}') == 400
    assert post_job(base, b'{"src": "https://example.com/v.mp4"}', origin="https://evil.example") == 403
    assert post_job(base, b'{"src": "https://example.com/v.mp4"}', origin=None) == 403
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(base + "/video/" + "b" * 24 + "/status")
    assert e.value.code == 404
