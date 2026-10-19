import io
import json
import shlex
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
    assert job.frames_censored == 8


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


def local_files(cfg, **video):
    # Local files, which the default protocol list refuses.
    return replace(cfg, video=replace(cfg.video, **video,
                                      source_protocols=(*cfg.video.source_protocols, "file")))


def segment_info(job, n):
    """ffprobe of segment n behind its run's init segment."""
    data = job.init_path(job.run_of[n][0]).read_bytes() + job.segment_path(n).read_bytes()
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                          "-count_packets", "-"], input=data, capture_output=True, check=True)
    return {s["codec_type"]: s for s in json.loads(out.stdout)["streams"]}


@needs_ffmpeg
def test_censor_video_end_to_end(cfg, tmp_path):
    src = tmp_path / "in.mp4"
    make_test_video(src)
    job = Job("t", str(src), out_dir=tmp_path / "out")
    fake = FakeDetector([det("FEMALE_BREAST_EXPOSED", box=(100, 80, 60, 60))])
    censor_video(job, local_files(cfg, segment_seconds=1), fake)
    # 10 fps sampled at 4/s -> every 2nd frame goes through detection, and
    # segments are a whole number of samples: 10 frames.
    assert (job.segment_frames, job.segment_seconds, job.segments) == (10, 1.0, 2)
    assert job.done == {0, 1} and job.seconds_done == 2.0 and job.runs == 1
    assert job.codecs.startswith("avc1.") and job.codecs.endswith(",mp4a.40.2")
    for n in (0, 1):
        info = segment_info(job, n)
        assert sorted(info) == ["audio", "video"] and info["video"]["nb_read_packets"] == "10"
    assert job.frames_censored == 20 and fake.calls == 10


@needs_ffmpeg
def test_seek_restarts_the_job_there_then_fills_the_gap(cfg, tmp_path):
    src = tmp_path / "in.mp4"
    make_test_video(src, seconds=6, audio=False)
    job = Job("t", str(src), out_dir=tmp_path / "out")
    calls = 0

    def detect(bgr):
        nonlocal calls
        calls += 1
        if calls == 3:  # early in segment 0, a viewer jumps to segment 4
            job.seek(4, wait_segments=1)
        return []
    censor_video(job, local_files(cfg, segment_seconds=1), FakeDetector(detect))
    assert job.segments == 6 and job.done == set(range(6))
    # Run 1 stopped at the seek, run 2 went from 4 to the end, and run 3
    # came back for what was skipped and stopped at segment 4, already done.
    assert job.runs == 3 and {n: job.run_of[n] for n in range(6)} == {
        0: (3, 0), 1: (3, 0), 2: (3, 0), 3: (3, 0), 4: (2, 4), 5: (2, 4)}
    assert not list(job.out_dir.glob("*.tmp"))
    for n in range(6):
        assert segment_info(job, n)["video"]["nb_read_packets"] == "10"


@needs_ffmpeg
def test_a_request_while_the_run_opens_the_source_does_not_restart_it(cfg, tmp_path, monkeypatch):
    src = tmp_path / "in.mp4"
    make_test_video(src, seconds=6, audio=False)
    job = Job("t", str(src), out_dir=tmp_path / "out")
    calls = asks = 0

    def detect(bgr):
        nonlocal calls
        calls += 1
        if calls == 3:
            job.seek(4, wait_segments=1)
        return []

    popen = subprocess.Popen

    def opening(cmd, *args, **kwargs):
        nonlocal asks
        if "-ss" in cmd and asks < 3:  # the player asks again while the source opens
            asks += 1
            job.seek(4, wait_segments=1)
        return popen(cmd, *args, **kwargs)
    monkeypatch.setattr("censor_proxy.video.subprocess.Popen", opening)
    censor_video(job, local_files(cfg, segment_seconds=1), FakeDetector(detect))
    assert asks == 1 and job.runs == 3 and job.done == set(range(6))
    assert job.run_of[4] == (2, 4)


def stalling_decoder(monkeypatch, after_frames):
    """The first run's decoder delivers after_frames frames, then hangs the
    way a stalled network read does (pipe open, nothing coming)."""
    popen = subprocess.Popen
    launched = 0

    def fake(cmd, *args, **kwargs):
        nonlocal launched
        if "rawvideo" in cmd and cmd[-1] == "pipe:1":  # a decoder
            launched += 1
            if launched == 1:
                w, h = cmd[cmd.index("-vf") + 1].removeprefix("scale=").split(":")
                size = after_frames * int(w) * int(h) * 3
                cmd = ["sh", "-c", f"{shlex.join(cmd)} | head -c {size}; exec sleep 600"]
        return popen(cmd, *args, **kwargs)
    monkeypatch.setattr("censor_proxy.video.subprocess.Popen", fake)


@needs_ffmpeg
def test_a_stalled_run_restarts_where_it_was(cfg, tmp_path, monkeypatch):
    src = tmp_path / "in.mp4"
    make_test_video(src, seconds=6, audio=False)
    job = Job("t", str(src), out_dir=tmp_path / "out")
    stalling_decoder(monkeypatch, after_frames=25)  # stalls in segment 2
    censor_video(job, local_files(cfg, segment_seconds=1, stall_seconds=1), FakeDetector([]))
    assert job.done == set(range(6)) and job.run_of[2] == (2, 2)


@needs_ffmpeg
def test_a_seek_stops_a_blocked_run(cfg, tmp_path, monkeypatch):
    src = tmp_path / "in.mp4"
    make_test_video(src, seconds=6, audio=False)
    job = Job("t", str(src), out_dir=tmp_path / "out")
    stalling_decoder(monkeypatch, after_frames=25)
    threading.Timer(1.0, job.seek, args=(4, 1)).start()
    started = time.monotonic()
    censor_video(job, local_files(cfg, segment_seconds=1, stall_seconds=0), FakeDetector([]))
    assert job.done == set(range(6)) and job.run_of[4] == (2, 4)
    assert time.monotonic() - started < 15


@needs_ffmpeg
def test_a_cancel_stops_a_blocked_run(cfg, tmp_path, monkeypatch):
    src = tmp_path / "in.mp4"
    make_test_video(src, seconds=6, audio=False)
    job = Job("t", str(src), out_dir=tmp_path / "out", state="running")
    stalling_decoder(monkeypatch, after_frames=25)
    threading.Timer(1.0, job.cancel, args=("cancelled: the page closed",)).start()
    with pytest.raises(VideoError, match="the page closed"):
        censor_video(job, local_files(cfg, segment_seconds=1, stall_seconds=0), FakeDetector([]))
    assert job.runs == 1


def test_seek_waits_when_the_job_is_close():
    job = Job("t", "x", segments=10, position=3, run_first=2)
    job.seek(5, wait_segments=2)
    assert job.restart_at is None  # it gets there soon enough
    job.seek(2, wait_segments=2)
    assert job.restart_at is None  # fed already; the encoder writes it once it has segment 3
    job.seek(6, wait_segments=2)
    assert job.restart_at == 6
    job.seek(1, wait_segments=2)
    assert job.restart_at == 1  # behind the run: it never goes back on its own
    job.take_restart()
    job.seek(12, wait_segments=2)  # past the end
    job.done.add(8)
    job.seek(8, wait_segments=2)  # already done
    assert job.restart_at is None
    unknown = Job("t", "x", position=0)  # length unknown: nothing to seek in
    unknown.seek(9, wait_segments=2)
    assert unknown.restart_at is None


def test_ready_needs_the_first_segments():
    job = Job("t", "x", state="running", segment_seconds=2.0, segments=5, codecs="avc1.64001f")
    assert not job.status(3)["ready"]
    job.done |= {0, 3}
    assert not job.status(3)["ready"]  # 2 s from the start
    job.done.add(1)
    assert job.status(3)["ready"]


def test_pump_stops_when_nobody_is_watching(cfg):
    w, h = 32, 32
    src = io.BytesIO(bytes(4 * w * h * 3))
    job = Job("t", "x", last_seen=time.time() - cfg.video.abandon_seconds - 1)
    with pytest.raises(VideoError, match="cancelled"):
        _pump(src, io.BytesIO(), w, h, w * h * 3, 10.0, 4, job, cfg, FakeDetector())


def test_startup_clears_earlier_outputs_only(cfg, tmp_path):
    old = tmp_path / f"{'a' * 24}-3.mp4"
    old_dir = tmp_path / f"{'b' * 24}-4"
    other = tmp_path / "holiday.mp4"
    old.write_bytes(b"x")
    old_dir.mkdir()
    (old_dir / "seg-00000.m4s").write_bytes(b"x")
    other.write_bytes(b"x")
    VideoJobs(replace(cfg, video=replace(cfg.video, cache_dir=str(tmp_path))), FakeDetector())
    assert not old.exists() and not old_dir.exists() and other.exists()


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
    job = Job("t", str(tmp_path / "missing.mp4"), out_dir=tmp_path / "out")
    with pytest.raises(Exception, match="cannot read source"):
        censor_video(job, cfg, FakeDetector())


@needs_ffmpeg
def test_local_files_refused_by_protocol_whitelist(cfg, tmp_path):
    # check_source refuses file: URLs up front; this is the backstop for
    # what a playlist references once ffmpeg is reading it.
    src = tmp_path / "in.mp4"
    make_test_video(src)
    job = Job("t", str(src), out_dir=tmp_path / "out")
    with pytest.raises(VideoError, match="cannot read source.*whitelist"):
        censor_video(job, cfg, FakeDetector())
    assert not job.out_dir.exists()


def test_start_takes_referer_from_origin_not_page(cfg, tmp_path, monkeypatch):
    cfg = replace(cfg, video=replace(cfg.video, cache_dir=str(tmp_path)))
    jobs = VideoJobs(cfg, FakeDetector())
    monkeypatch.setattr("censor_proxy.video.censor_video", lambda *a: None)
    job = jobs.start("https://93.184.215.14/v.mp4", "https://www.reddit.com", "UA/1",
                     page="https://www.reddit.com/r/x/comments/1")
    assert job.referer == "https://www.reddit.com/" and job.page == "https://www.reddit.com/r/x/comments/1"
    assert jobs.start("https://93.184.215.14/w.mp4").referer is None


def test_job_ids_are_random_and_per_origin(cfg, tmp_path, monkeypatch):
    # A page that knows a video URL must not be able to derive the id of a
    # job another site started, nor get a cache hit that tells it ran.
    cfg = replace(cfg, video=replace(cfg.video, cache_dir=str(tmp_path)))
    monkeypatch.setattr("censor_proxy.video.censor_video", lambda *a: None)
    src = "https://93.184.215.14/v.mp4"
    a = VideoJobs(cfg, FakeDetector()).start(src, "https://a.example")
    jobs = VideoJobs(cfg, FakeDetector())
    b = jobs.start(src, "https://a.example")
    assert a.id != b.id and len(b.id) == 24  # not a function of the source
    assert jobs.start(src, "https://a.example") is b  # same page origin: reused
    c = jobs.start(src, "https://c.example")
    assert c is not b and c.id != b.id  # another origin: its own job
    assert jobs.get(b.id) is b and jobs.get(c.id) is c


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
    # Other spellings of loopback: IPv4-mapped IPv6, hex and decimal.
    "http://[::ffff:127.0.0.1]/v.mp4", "http://0x7f000001/v.mp4", "http://2130706433/v.mp4",
    "http://169.254.169.254/latest/meta-data", "http://0.0.0.0/v.mp4",
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
    assert b'"true" === "true";      // video.start_muted' in body
    assert b'Number("400")' in body  # images.placeholder_delay_ms
    # video.ad_sources: a JSON list inside a JS string literal.
    assert b'JSON.parse("[\\"/pre_videos/\\"]")' in body


def test_server_serves_segments_as_they_are_done(server, tmp_path):
    srv, jobs, base = server
    job = Job("a" * 24, "https://example.com/v.mp4", out_dir=tmp_path / "v", state="running",
              segment_seconds=2.0, segments=3, position=1)
    job.out_dir.mkdir()
    jobs._jobs[job.id] = job
    job.init_path(1).write_bytes(b"init1")
    job.init_path(2).write_bytes(b"init2")
    job.segment_path(0).write_bytes(b"seg0")
    job.run_of[0] = (1, 0)
    origin = {"Origin": "https://www.reddit.com"}
    status, headers, body = get(f"{base}/video/{job.id}/segment/0", origin)
    assert status == 200 and body == b"seg0"
    assert (headers["X-Init"], headers["X-Offset"]) == ("1", "0.000000")
    assert headers["Access-Control-Allow-Origin"] == "https://www.reddit.com"
    assert "X-Init" in headers["Access-Control-Expose-Headers"]
    assert get(f"{base}/video/{job.id}/init/2")[2] == b"init2"

    # Not done yet: the request waits for it (the job is working on it, so
    # no restart).
    def finish():
        job.run_of[1] = (2, 1)
        job.segment_path(1).write_bytes(b"seg1")
        job.update()
    threading.Timer(0.3, finish).start()
    status, headers, body = get(f"{base}/video/{job.id}/segment/1")
    assert body == b"seg1" and headers["X-Offset"] == "2.000000" and job.restart_at is None
    status, _, body = get(f"{base}/video/{job.id}/segment/3")
    assert status == 204 and body == b""  # past the end

    job.update(state="error", error="boom")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"{base}/video/{job.id}/segment/2")
    assert e.value.code == 409


def test_server_segment_request_restarts_the_job(server, monkeypatch):
    srv, jobs, base = server
    monkeypatch.setattr("censor_proxy.server.SEGMENT_WAIT", 0.2)
    job = Job("f" * 24, "https://example.com/v.mp4", state="running", segment_seconds=4.0,
              segments=100, position=2)
    jobs._jobs[job.id] = job
    status, _, body = get(f"{base}/video/{job.id}/segment/4")  # within seek_wait_seconds
    assert status == 202 and job.restart_at is None
    status, _, body = get(f"{base}/video/{job.id}/segment/50")
    assert status == 202 and job.restart_at == 50


def test_server_only_the_newest_segment_request_seeks(server, monkeypatch, tmp_path):
    srv, jobs, base = server
    monkeypatch.setattr("censor_proxy.server.SEGMENT_WAIT", 1.5)
    job = Job("e" * 24, "https://example.com/v.mp4", out_dir=tmp_path / "v", state="running",
              segment_seconds=4.0, segments=100, position=2)
    job.out_dir.mkdir()
    jobs._jobs[job.id] = job
    job.segment_path(3).write_bytes(b"seg3")
    job.run_of[3] = (1, 0)
    # The viewer jumps to segment 50, then back to 3 before it is ready.
    # Their fetch for 50 is dropped, but the server is still waiting on it.
    stale = threading.Thread(target=get, args=(f"{base}/video/{job.id}/segment/50",))
    stale.start()
    for _ in range(50):
        if job.restart_at == 50:
            break
        time.sleep(0.02)
    assert get(f"{base}/video/{job.id}/segment/3")[2] == b"seg3"
    job.take_restart()  # the job goes to 50...
    job.update(position=None)
    time.sleep(0.3)
    assert job.restart_at is None  # ...and the dropped request doesn't ask for it again
    stale.join()


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
    assert body["status_url"] == f"/video/{'d' * 24}/status"
    assert calls == [("https://example.com/v.mp4", "https://www.reddit.com", "UA/1",
                      "https://www.reddit.com/r/x")]

    def refuse(*a, **k):
        raise JobRefused("too many")
    monkeypatch.setattr(jobs, "start", refuse)
    assert post_job(base, b'{"src": "https://example.com/v.mp4"}') == 429


def test_debug_beacon_needs_allowlisted_origin(server, monkeypatch):
    srv, jobs, base = server
    monkeypatch.setattr(srv, "cfg", replace(srv.cfg, video=replace(srv.cfg.video, debug=True)))

    def post(origin):
        req = urllib.request.Request(base + "/debug", data=b'{"event": "x"}', method="POST",
                                     headers={"Origin": origin} if origin else {})
        try:
            return urllib.request.urlopen(req).status
        except urllib.error.HTTPError as e:
            return e.code
    assert post("https://evil.example") == 403 and post(None) == 403
    assert post("https://www.reddit.com") == 204


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
    assert jobs._take_slot()
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
    jobs._release_slot()  # the paused job's pump stops and frees the slot
    worker.join(5)
    assert ran.is_set() and waiting.state == "done"


def test_waiting_job_in_background_does_not_preempt(cfg, tmp_path):
    cfg = replace(cfg, video=replace(cfg.video, cache_dir=str(tmp_path), max_concurrent_jobs=1))
    jobs = VideoJobs(cfg, FakeDetector())
    paused = Job("p" * 24, "https://example.com/a.mp4", state="running", last_active=0)
    jobs._jobs[paused.id] = paused
    assert jobs._take_slot()
    hidden = Job("h" * 24, "https://example.com/b.mp4", last_active=0)
    worker = threading.Thread(target=jobs._wait_for_slot, args=(hidden,))
    worker.start()
    time.sleep(1.2)
    hidden.cancel("test over")
    worker.join(5)
    assert not paused.cancel_reason


def test_job_limit_follows_the_config(cfg, tmp_path):
    """Raising max_concurrent_jobs (a resource level switch) starts a waiting
    job at once; lowering it lets running jobs finish but starts no more."""
    one = replace(cfg, video=replace(cfg.video, cache_dir=str(tmp_path), max_concurrent_jobs=1))
    jobs = VideoJobs(one, FakeDetector())
    assert jobs._take_slot()
    waiting = Job("w" * 24, "https://example.com/b.mp4")
    got = threading.Event()
    worker = threading.Thread(target=lambda: jobs._wait_for_slot(waiting) and got.set())
    worker.start()
    assert not got.wait(0.7)
    jobs.reconfigure(replace(one, video=replace(one.video, max_concurrent_jobs=2)))
    assert got.wait(5)
    worker.join(5)
    jobs.reconfigure(one)
    jobs._release_slot()
    assert not jobs._take_slot()  # one still running, limit 1
    jobs._release_slot()
    assert jobs._take_slot()


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
