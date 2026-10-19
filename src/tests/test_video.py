import io
import json
import shutil
import subprocess
import threading
import time
import urllib.request
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from conftest import FakeDetector, det

from censor_proxy.server import InternalServer
from censor_proxy.video import (Job, JobRefused, Probe, VideoError, VideoJobs, _net_args, _pump,
                                censor_video, check_source, output_size)

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def test_output_size_caps_height_and_keeps_even():
    assert output_size(Probe(0, 1920, 1080, 30, 10, None), 720) == (1280, 720)
    assert output_size(Probe(0, 801, 1423, 30, 10, None), 720) == (404, 720)
    assert output_size(Probe(0, 640, 360, 30, 10, None), 720) == (640, 360)


def test_net_args_send_browser_like_requests():
    # Pornhub's CDN answers 410 when User-Agent precedes Host (what
    # -user_agent does) or when an HLS request carries "Range: bytes=0-".
    job = Job("t", "https://cdn.example/v/master.m3u8?h=x", referer="https://site.example/p",
              user_agent="UA/1")
    assert _net_args(job) == ["-headers", "User-Agent: UA/1\r\nReferer: https://site.example/p\r\n",
                              "-seekable", "0", "-http_seekable", "0"]
    # Files stay seekable: an MP4 with its index at the end needs it.
    assert _net_args(Job("t", "https://cdn.example/v.mp4?x.m3u8")) == []
    # The protocol whitelist goes first (it must precede -i).
    assert _net_args(job, ("https", "http"))[:2] == ["-protocol_whitelist", "https,http"]


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
    # A local file, which the default protocol list refuses.
    cfg = replace(cfg, video=replace(cfg.video, source_protocols=(*cfg.video.source_protocols, "file")))
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


def test_pump_stops_when_nobody_is_watching(cfg):
    w, h = 32, 32
    src = io.BytesIO(bytes(4 * w * h * 3))
    job = Job("t", "x", last_seen=time.time() - cfg.video.abandon_seconds - 1)
    with pytest.raises(VideoError, match="cancelled"):
        _pump(src, io.BytesIO(), w, h, w * h * 3, 10.0, 4, job, cfg, FakeDetector())


def test_startup_clears_earlier_outputs_only(cfg, tmp_path):
    old = tmp_path / f"{'a' * 24}-3.mp4"
    other = tmp_path / "holiday.mp4"
    old.write_bytes(b"x")
    other.write_bytes(b"x")
    VideoJobs(replace(cfg, video=replace(cfg.video, cache_dir=str(tmp_path))), FakeDetector())
    assert not old.exists() and other.exists()


def test_abandoned_queued_job_never_runs(cfg, tmp_path, monkeypatch):
    cfg = replace(cfg, video=replace(cfg.video, cache_dir=str(tmp_path)))
    jobs = VideoJobs(cfg, FakeDetector())
    monkeypatch.setattr("censor_proxy.video.censor_video",
                        lambda *a: pytest.fail("abandoned job was run"))
    job = Job("a" * 24, "https://example.com/v.mp4", last_seen=time.time() - 3600)
    jobs._run(job)
    assert job.state == "error" and "cancelled" in job.error


@needs_ffmpeg
def test_censor_video_rejects_unreadable_source(cfg, tmp_path):
    job = Job("t", str(tmp_path / "missing.mp4"), path=tmp_path / "out.mp4")
    with pytest.raises(Exception, match="cannot read source"):
        censor_video(job, cfg, FakeDetector())


@needs_ffmpeg
def test_local_files_refused_by_protocol_whitelist(cfg, tmp_path):
    # check_source refuses file: URLs up front; this is the backstop for
    # what a playlist references once ffmpeg is reading it.
    src = tmp_path / "in.mp4"
    make_test_video(src)
    job = Job("t", str(src), path=tmp_path / "out.mp4")
    with pytest.raises(VideoError, match="cannot read source.*whitelist"):
        censor_video(job, cfg, FakeDetector())
    assert not job.path.exists()


def test_start_takes_referer_from_origin_not_page(cfg, tmp_path, monkeypatch):
    cfg = replace(cfg, video=replace(cfg.video, cache_dir=str(tmp_path)))
    jobs = VideoJobs(cfg, FakeDetector())
    monkeypatch.setattr("censor_proxy.video.censor_video", lambda *a: None)
    job = jobs.start("https://93.184.215.14/v.mp4", "https://www.reddit.com", "UA/1",
                     page="https://www.reddit.com/r/x/comments/1")
    assert job.referer == "https://www.reddit.com/" and job.page == "https://www.reddit.com/r/x/comments/1"
    assert jobs.start("https://93.184.215.14/w.mp4").referer is None


def test_queue_cap_refuses_further_jobs(cfg, tmp_path, monkeypatch):
    cfg = replace(cfg, video=replace(cfg.video, cache_dir=str(tmp_path), max_concurrent_jobs=1,
                                     max_queued_jobs=1))
    jobs = VideoJobs(cfg, FakeDetector())
    release = threading.Event()
    monkeypatch.setattr("censor_proxy.video.censor_video", lambda *a: release.wait(5))
    running = jobs.start("https://93.184.215.14/a.mp4")
    for _ in range(40):
        if running.state == "running":
            break
        time.sleep(0.05)
    assert running.state == "running"
    waiting = jobs.start("https://93.184.215.14/b.mp4")
    assert waiting.state == "queued"
    with pytest.raises(JobRefused, match="already waiting"):
        jobs.start("https://93.184.215.14/c.mp4")
    assert jobs.start("https://93.184.215.14/b.mp4") is waiting  # a known job is never refused
    release.set()


def test_rate_limit_is_per_origin(cfg, tmp_path, monkeypatch):
    cfg = replace(cfg, video=replace(cfg.video, cache_dir=str(tmp_path), max_starts_per_minute=2))
    jobs = VideoJobs(cfg, FakeDetector())
    monkeypatch.setattr("censor_proxy.video.censor_video", lambda *a: None)
    jobs.start("https://93.184.215.14/a.mp4", "https://a.example")
    jobs.start("https://93.184.215.14/a.mp4", "https://a.example")  # repeats count too
    with pytest.raises(JobRefused, match="too many"):
        jobs.start("https://93.184.215.14/b.mp4", "https://a.example")
    jobs.start("https://93.184.215.14/b.mp4", "https://b.example")


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
    status, headers, body = get(base + "/health", {"Origin": "https://www.reddit.com"})
    assert json.loads(body) == {"ready": True}
    # Never readable cross-origin: a page can't use it to tell the proxy is there.
    assert "Access-Control-Allow-Origin" not in headers
    status, headers, body = get(base + "/inject.js", {"Origin": "https://www.reddit.com"})
    assert "Access-Control-Allow-Origin" not in headers
    assert headers["Content-Type"].startswith("text/javascript")
    assert b"__censorProxy" not in body and b"currentScript.remove()" in body
    assert b'"https://censor.local"' in body and b"__CENSOR_ORIGIN__" not in body
    assert b'"Sissy Censor System"' in body
    assert b'"__' not in body  # every template value filled in
    assert b'"true" === "true";                  // video.enabled' in body
    assert b'Number("400")' in body  # images.placeholder_delay_ms


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


def test_server_cors_only_for_allowlisted_origins(server):
    srv, jobs, base = server
    job = Job("e" * 24, "https://example.com/v.mp4", state="running")
    jobs._jobs[job.id] = job
    status, headers, _ = get(f"{base}/video/{job.id}/status", {"Origin": "https://www.reddit.com"})
    assert headers["Access-Control-Allow-Origin"] == "https://www.reddit.com"
    assert headers["Vary"] == "Origin"
    status, headers, _ = get(f"{base}/video/{job.id}/status", {"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in headers
    status, headers, _ = get(f"{base}/video/{job.id}/status")
    assert "Access-Control-Allow-Origin" not in headers


def test_server_passes_origin_and_page_to_jobs(server, monkeypatch):
    srv, jobs, base = server
    calls = []

    def fake_start(src, origin=None, user_agent=None, page=None):
        calls.append((src, origin, user_agent, page))
        return Job("d" * 24, src, state="running")
    monkeypatch.setattr(jobs, "start", fake_start)
    req = urllib.request.Request(
        base + "/video/jobs", method="POST",
        data=b'{"src": "https://example.com/v.mp4", "page": "https://www.reddit.com/r/x"}',
        headers={"Origin": "https://www.reddit.com", "User-Agent": "UA/1"})
    with urllib.request.urlopen(req) as r:
        headers, body = dict(r.headers), json.loads(r.read())
    assert headers["Access-Control-Allow-Origin"] == "https://www.reddit.com"
    assert body["stream_url"] == f"/video/{'d' * 24}/stream"
    assert calls == [("https://example.com/v.mp4", "https://www.reddit.com", "UA/1",
                      "https://www.reddit.com/r/x")]

    def refuse(*a, **k):
        raise JobRefused("too many")
    monkeypatch.setattr(jobs, "start", refuse)
    assert post_job(base, b'{"src": "https://example.com/v.mp4"}') == 429


def test_server_job_status_and_errors(server):
    srv, jobs, base = server
    assert post_job(base, b'{"src": "ftp://x"}') == 400
    assert post_job(base, b'{"src": "https://example.com/v.mp4"}', origin="https://evil.example") == 403
    assert post_job(base, b'{"src": "https://example.com/v.mp4"}', origin=None) == 403
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(base + "/video/" + "b" * 24 + "/status")
    assert e.value.code == 404


def test_paused_job_yields_its_slot_to_a_waiting_one(cfg, tmp_path, monkeypatch):
    cfg = replace(cfg, video=replace(cfg.video, cache_dir=str(tmp_path), max_concurrent_jobs=1))
    jobs = VideoJobs(cfg, FakeDetector())
    # A running job whose video has been paused past idle_seconds holds the slot.
    paused = Job("p" * 24, "https://example.com/a.mp4", state="running",
                 last_active=time.time() - cfg.video.idle_seconds - 1)
    jobs._jobs[paused.id] = paused
    jobs._slots.acquire()
    ran = threading.Event()
    monkeypatch.setattr("censor_proxy.video.censor_video", lambda *a: ran.set())
    waiting = Job("w" * 24, "https://example.com/b.mp4")
    jobs._jobs[waiting.id] = waiting
    worker = threading.Thread(target=jobs._run, args=(waiting,))
    worker.start()
    deadline = time.time() + 5
    while not paused.cancel_reason and time.time() < deadline:
        waiting.touch(active=True)  # its page is polling, visible
        time.sleep(0.1)
    assert "paused" in paused.cancel_reason
    jobs._slots.release()  # the paused job's pump stops and frees the slot
    worker.join(5)
    assert ran.is_set() and waiting.state == "done"


def test_waiting_job_in_background_does_not_preempt(cfg, tmp_path):
    cfg = replace(cfg, video=replace(cfg.video, cache_dir=str(tmp_path), max_concurrent_jobs=1))
    jobs = VideoJobs(cfg, FakeDetector())
    paused = Job("p" * 24, "https://example.com/a.mp4", state="running", last_active=0)
    jobs._jobs[paused.id] = paused
    jobs._slots.acquire()
    hidden = Job("h" * 24, "https://example.com/b.mp4", last_active=0)
    worker = threading.Thread(target=jobs._wait_for_slot, args=(hidden,))
    worker.start()
    time.sleep(1.2)
    hidden.cancel("test over")
    worker.join(5)
    assert not paused.cancel_reason


def test_server_status_activity_and_cancel(server):
    srv, jobs, base = server
    job = Job("c" * 24, "https://example.com/v.mp4", state="running", last_active=0)
    jobs._jobs[job.id] = job
    get(f"{base}/video/{job.id}/status?active=0")
    assert job.last_active == 0 and time.time() - job.last_seen < 5
    get(f"{base}/video/{job.id}/status?active=1")
    assert time.time() - job.last_active < 5

    def cancel(origin):
        req = urllib.request.Request(f"{base}/video/{job.id}/cancel", data=b"", method="POST",
                                     headers={"Origin": origin})
        try:
            return urllib.request.urlopen(req).status
        except urllib.error.HTTPError as e:
            return e.code
    assert cancel("https://evil.example") == 403 and not job.cancel_reason
    assert cancel("https://www.reddit.com") == 200 and "closed" in job.cancel_reason
