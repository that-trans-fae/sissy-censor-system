"""Video-level sex attribution: decides, stretch by stretch, whether the
subject of a video is male, from evidence across time rather than per frame.

Per-frame rules can't tell a male chest the model reads as female from a
real breast: the frame often shows nothing else (see docs/decisions.md).
So a scanner decodes the video a second time at a low rate
(video_attribution.sample_fps), runs the attribution model (640m by
default, which sees male faces and penises far more reliably than 320n) on
each frame, and records whether it shows confident male or female
evidence. The job's pump waits until the scan is lookahead_seconds ahead of
the frame it is about to censor, then asks whether that moment is in a male
stretch: enough male evidence and no female evidence in the window around
it. There, the labels in pass_labels are dropped from the detection config,
so misread breasts pass and stop corroborating each other.

Anything that goes wrong with the scan (decode error, stall, model error)
leaves the job censoring as if attribution were off.
"""

from __future__ import annotations

import bisect
import subprocess
import threading
import time
from dataclasses import dataclass, replace

import cv2
import numpy as np

from .config import DetectionConfig, VideoAttributionConfig
from .detection import Detection, Detector


@dataclass(frozen=True)
class Evidence:
    t: float
    male: bool
    female: bool


def evidence(t: float, detections: list[Detection], cfg: VideoAttributionConfig) -> Evidence:
    return Evidence(
        t,
        male=any(d.label in cfg.male_labels and d.score >= cfg.male_threshold for d in detections),
        female=any(d.label in cfg.female_labels and d.score >= cfg.female_threshold
                   for d in detections),
    )


def male_detection(detection: DetectionConfig, cfg: VideoAttributionConfig) -> DetectionConfig:
    """The detection config for a male stretch: pass_labels are neither
    censored nor female context."""
    return replace(detection,
                   female_labels=detection.female_labels - cfg.pass_labels,
                   female_context_labels=detection.female_context_labels - cfg.pass_labels)


class Timeline:
    """Evidence in time order, added by the scanner and read by the pump.
    Thread-safe."""

    def __init__(self, cfg: VideoAttributionConfig):
        self.cfg = cfg
        self._times: list[float] = []
        self._evidence: list[Evidence] = []
        # complete: the scan reached the end of the video. failed: it stopped
        # early; nothing past what it covered counts as male.
        self.complete = False
        self.failed = ""
        self._progress = time.monotonic()
        self._changed = threading.Condition()

    def add(self, ev: Evidence) -> None:
        with self._changed:
            self._times.append(ev.t)
            self._evidence.append(ev)
            self._progress = time.monotonic()
            self._changed.notify_all()

    def finish(self, failed: str = "") -> None:
        with self._changed:
            if not (self.complete or self.failed):
                self.complete, self.failed = not failed, failed
            self._changed.notify_all()

    def _covered(self, t: float) -> bool:
        return self.complete or bool(self._times and self._times[-1] >= t)

    def wait(self, t: float, timeout: float) -> bool:
        """Wait up to `timeout` for the scan to cover time `t`. True once it
        has, or once it has ended; then male_at() can answer. A scan making
        no progress for stall_seconds is given up on."""
        with self._changed:
            self._changed.wait_for(lambda: self._covered(t) or self.failed, timeout)
            if not (self._covered(t) or self.failed) and \
                    time.monotonic() - self._progress > self.cfg.stall_seconds:
                self.failed = f"no progress for {self.cfg.stall_seconds:g}s"
            return self._covered(t) or bool(self.failed)

    def male_at(self, t: float) -> bool:
        """Whether time `t` is in a male stretch: at least min_male_samples
        male and at most max_female_samples female samples from
        history_seconds before to lookahead_seconds after it."""
        c = self.cfg
        with self._changed:
            if not self._covered(t + c.lookahead_seconds):
                return False  # the scan failed before seeing what comes next
            lo = bisect.bisect_left(self._times, t - c.history_seconds)
            hi = bisect.bisect_right(self._times, t + c.lookahead_seconds)
            window = self._evidence[lo:hi]
        males = sum(e.male for e in window)
        females = sum(e.female for e in window)
        return males >= c.min_male_samples and females <= c.max_female_samples


class Scanner:
    """Runs `cmd`, an ffmpeg command writing raw RGB frames of w x h at
    cfg.sample_fps to stdout, through `detector` on a thread, filling a
    Timeline. `t0` is the video time of the first frame (a job restarted
    where the viewer seeked to)."""

    def __init__(self, cmd: list[str], w: int, h: int, cfg: VideoAttributionConfig,
                 detector: Detector, name: str = "attribution", t0: float = 0.0):
        self.cmd, self.w, self.h, self.cfg, self.detector = cmd, w, h, cfg, detector
        self.t0 = t0
        self.timeline = Timeline(cfg)
        self._proc: subprocess.Popen | None = None
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    def start(self) -> Timeline:
        self._thread.start()
        return self.timeline

    def stop(self) -> None:
        self._stopped.set()
        if self._proc and self._proc.poll() is None:
            self._proc.kill()
        self._thread.join(timeout=30)
        self.timeline.finish("stopped")

    def _run(self) -> None:
        frame_bytes = self.w * self.h * 3
        try:
            self._proc = subprocess.Popen(self.cmd, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL)
            if self._stopped.is_set():
                self._proc.kill()
            k = 0
            while not self._stopped.is_set():
                buf = self._proc.stdout.read(frame_bytes)
                if len(buf) < frame_bytes:
                    break
                frame = np.frombuffer(buf, np.uint8).reshape(self.h, self.w, 3)
                found = self.detector.detect(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
                self.timeline.add(evidence(self.t0 + k / self.cfg.sample_fps, found, self.cfg))
                k += 1
            self._proc.stdout.close()
            rc = self._proc.wait(timeout=30)
            if self._stopped.is_set():
                return
            self.timeline.finish("" if rc == 0 else f"decode failed (exit {rc})")
        except Exception as e:
            self.timeline.finish(str(e) or type(e).__name__)
        finally:
            # The pump logs a failure, with the job and the time it hit.
            if self._proc and self._proc.poll() is None:
                self._proc.kill()
