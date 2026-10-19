import io
import shutil
import time
from dataclasses import replace

import numpy as np
import pytest
from conftest import FakeDetector, det

from censor_proxy.attribution import Evidence, Scanner, Timeline, evidence, male_detection
from censor_proxy.config import ConfigError
from censor_proxy.detection import decide
from censor_proxy.video import Job, VideoJobs, _pump, censor_video
from test_video import make_test_video

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")

BREAST = (8, 8, 16, 16)


@pytest.fixture
def acfg_cfg(cfg):
    a = replace(cfg.video_attribution, enabled=True, sample_fps=4, history_seconds=2,
                lookahead_seconds=1, min_male_samples=2, max_female_samples=0)
    return replace(cfg, video_attribution=a)


def timeline(a, marks, complete=True):
    """A Timeline with one sample per entry of `marks` at a.sample_fps:
    'm' male, 'f' female, 'b' both, anything else neither."""
    tl = Timeline(a)
    for k, m in enumerate(marks):
        tl.add(Evidence(k / a.sample_fps, m in "mb", m in "fb"))
    if complete:
        tl.finish()
    return tl


def test_defaults_ship_off(cfg):
    a = cfg.video_attribution
    assert not a.enabled and a.model_path == "/app/models/640m.onnx"
    assert a.pass_labels == {"FEMALE_BREAST_EXPOSED"}
    assert VideoJobs.attribution_detector.fget(VideoJobs(replace(
        cfg, video=replace(cfg.video, cache_dir="/tmp/censor-test")), FakeDetector())) is None


@pytest.mark.parametrize("text", [
    "video_attribution: {sample_fps: 0}",
    "video_attribution: {lookahead_seconds: -1}",
    "video_attribution: {min_male_samples: 0}",
    "video_attribution: {max_female_samples: -1}",
    "video_attribution: {stall_seconds: 0}",
    "video_attribution: {nope: 1}",
])
def test_bad_attribution_config_rejected(make_config, text):
    with pytest.raises(ConfigError):
        make_config(text)


def test_evidence_uses_labels_and_thresholds(cfg):
    a = cfg.video_attribution
    assert evidence(0, [det("MALE_GENITALIA_EXPOSED", a.male_threshold)], a) == Evidence(0, True, False)
    assert evidence(0, [det("MALE_GENITALIA_EXPOSED", a.male_threshold - 0.01)], a).male is False
    assert evidence(0, [det("FACE_FEMALE", a.female_threshold)], a).female is True
    # Breasts are what's being attributed, so they are neither.
    assert evidence(0, [det("FEMALE_BREAST_EXPOSED", 0.9)], a) == Evidence(0, False, False)


def test_male_detection_passes_breasts_but_not_genitalia(cfg):
    male = male_detection(cfg.detection, cfg.video_attribution)
    pair = [det("FEMALE_BREAST_EXPOSED", 0.9, (10, 10, 20, 20)),
            det("FEMALE_BREAST_EXPOSED", 0.4, (35, 10, 20, 20))]
    assert all(d.censor for d in decide(pair, cfg.detection))
    assert not any(d.censor for d in decide(pair, male))
    assert all(d.censor for d in decide([det("FEMALE_GENITALIA_EXPOSED", 0.9)], male))
    # Buttocks next to a passed breast and a male face aren't female-attributed.
    ctx = [det("BUTTOCKS_EXPOSED", 0.9, (100, 100, 40, 40)), det("FEMALE_BREAST_EXPOSED", 0.9, (100, 60, 30, 30)),
           det("FACE_MALE", 0.9, (150, 100, 30, 30))]
    assert decide(ctx, cfg.detection)[0].censor and not decide(ctx, male)[0].censor


def test_window_needs_male_evidence_and_no_female(acfg_cfg):
    a = acfg_cfg.video_attribution  # 4 samples/s, window -2 s .. +1 s, 2 male, 0 female
    tl = timeline(a, "....mm......")
    assert not tl.male_at(0.0)       # window 0..1 s: sample 4 (1.0 s) only
    assert tl.male_at(0.5)           # both male samples in view
    assert tl.male_at(3.0)           # 1.0..4.0 s: still both
    assert not tl.male_at(3.1)       # 1.1..: the one at 1.0 s has dropped out
    # One female sample anywhere in the window vetoes, before it appears too.
    tl = timeline(a, "mmmmmmmmfmmmmmmmmmmm")
    assert not tl.male_at(1.5)       # the female sample at 2.0 s is in the lookahead
    assert tl.male_at(0.75) and not tl.male_at(3.5) and tl.male_at(4.25)


def test_failed_scan_stops_attributing_past_what_it_saw(acfg_cfg):
    a = acfg_cfg.video_attribution
    tl = timeline(a, "mmmmmmmm", complete=False)   # covers 0..1.75 s
    tl.finish("decode failed")
    assert tl.wait(10, 0) and tl.male_at(0.5)
    assert not tl.male_at(1.0)                      # 2.0 s of lookahead unseen
    done = timeline(a, "mmmmmmmm")
    assert done.male_at(1.5)                        # a complete scan has seen the end


def test_wait_gives_up_on_a_stalled_scan(acfg_cfg):
    a = replace(acfg_cfg.video_attribution, stall_seconds=0.2)
    tl = Timeline(a)
    assert not tl.wait(1, 0.05)
    time.sleep(0.25)
    assert tl.wait(1, 0.05) and "no progress" in tl.failed and not tl.male_at(0)


def run_pump(cfg, n, tl, step=4, fps=10.0, w=64, h=48):
    src = io.BytesIO(np.random.default_rng(0).integers(0, 256, (n, h, w, 3), dtype=np.uint8).tobytes())
    dst = io.BytesIO()
    job = Job("t", "x")
    _pump(src, dst, w, h, w * h * 3, fps, step, job, cfg,
          FakeDetector([det("FEMALE_BREAST_EXPOSED", box=BREAST)]), tl)
    return job


def test_pump_passes_breasts_in_a_male_stretch(acfg_cfg):
    a = acfg_cfg.video_attribution
    job = run_pump(acfg_cfg, 20, timeline(a, "m" * 12))
    assert job.frames_censored == 0 and job.seconds_male == pytest.approx(2.0)
    # Female evidence 1 s in: censored from the lookahead before it on.
    job = run_pump(acfg_cfg, 20, timeline(a, "mmmmfmmm" + "m" * 4))
    assert job.frames_censored == 20 and job.seconds_male == 0
    # No timeline: censored as before.
    assert run_pump(acfg_cfg, 20, None).frames_censored == 20


def test_pump_stops_waiting_when_nobody_is_watching(acfg_cfg):
    job = Job("t", "x", last_seen=time.time() - acfg_cfg.video.abandon_seconds - 1)
    with pytest.raises(Exception, match="cancelled"):
        _pump(io.BytesIO(bytes(4 * 32 * 32 * 3)), io.BytesIO(), 32, 32, 32 * 32 * 3, 10.0, 4, job,
              acfg_cfg, FakeDetector(), Timeline(acfg_cfg.video_attribution))


@needs_ffmpeg
def test_scanner_reads_at_its_own_rate(acfg_cfg, tmp_path):
    src = tmp_path / "in.mp4"
    make_test_video(src, seconds=2, audio=False)
    a = acfg_cfg.video_attribution
    fake = FakeDetector([det("FACE_MALE", 0.9)])
    cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(src), "-vf", "fps=4,scale=64:48",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    scanner = Scanner(cmd, 64, 48, a, fake)
    tl = scanner.start()
    assert tl.wait(99, 10) and tl.complete and not tl.failed
    scanner.stop()
    assert tl.complete and fake.calls == 8 and tl.male_at(1.0)
    # A run restarted mid-video scans from there: evidence at video time.
    scanner = Scanner(cmd, 64, 48, a, fake, t0=30.0)
    tl = scanner.start()
    assert tl.wait(99, 10) and tl.complete
    scanner.stop()
    assert tl._times[0] == 30.0 and tl._times[-1] == 31.75


@needs_ffmpeg
def test_scanner_failure_censors_as_before(acfg_cfg):
    scanner = Scanner(["ffmpeg", "-loglevel", "error", "-i", "/nonexistent.mp4", "-f", "rawvideo", "pipe:1"],
                      64, 48, acfg_cfg.video_attribution, FakeDetector())
    tl = scanner.start()
    assert tl.wait(1, 10) and "decode failed" in tl.failed
    scanner.stop()
    assert not tl.male_at(0)


@needs_ffmpeg
def test_censor_video_with_attribution(acfg_cfg, tmp_path):
    src = tmp_path / "in.mp4"
    make_test_video(src)
    breast = FakeDetector([det("FEMALE_BREAST_EXPOSED", box=(100, 80, 60, 60))])
    job = Job("t", str(src), out_dir=tmp_path / "out")
    # A local file, which the default protocol list refuses.
    acfg_cfg = replace(acfg_cfg, video=replace(acfg_cfg.video, source_protocols=(
        *acfg_cfg.video.source_protocols, "file")))
    censor_video(job, acfg_cfg, breast, FakeDetector([det("MALE_GENITALIA_EXPOSED", 0.9)]))
    assert job.frames_censored == 0 and job.seconds_male == pytest.approx(2.0)
    job = Job("t", str(src), out_dir=tmp_path / "out2")
    censor_video(job, acfg_cfg, breast, FakeDetector([det("MALE_GENITALIA_EXPOSED", 0.9),
                                                       det("FACE_FEMALE", 0.9)]))
    assert job.frames_censored == 20 and job.seconds_male == 0
