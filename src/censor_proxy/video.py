"""Video censoring jobs: ffmpeg decode -> detect on sampled frames -> blur ->
ffmpeg encode to fragmented MP4, written to disk as it is produced so
playback can start before the job finishes.

ffmpeg reads plain files, HLS and DASH natively, so one pipeline covers all
three. Boxes found on a sampled frame are applied to every frame up to the
next sample, together with that sample's boxes, so motion between samples
stays covered (the same idea as GIF frame sampling).
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

import cv2
import numpy as np

from .config import Config
from .detection import Box, Detector, decide
from .imaging import apply_censor

log = logging.getLogger("censor_proxy.video")


class VideoError(RuntimeError):
    pass


@dataclass(frozen=True)
class Probe:
    video_index: int
    width: int
    height: int
    fps: float
    duration: float | None
    audio_index: int | None


@dataclass
class Job:
    id: str
    src: str
    referer: str | None = None
    user_agent: str | None = None
    # queued | running | done | error
    state: str = "queued"
    error: str = ""
    duration: float | None = None
    seconds_done: float = 0.0
    frames_censored: int = 0
    created: float = field(default_factory=time.time)
    finished: float | None = None
    path: Path | None = None
    changed: threading.Condition = field(default_factory=threading.Condition, repr=False)

    def status(self, ready_seconds: float) -> dict:
        ready = self.state == "done" or (self.state == "running" and self.seconds_done >= ready_seconds)
        return {
            "id": self.id,
            "state": self.state,
            "ready": ready,
            "error": self.error,
            "duration": self.duration,
            "seconds_done": round(self.seconds_done, 1),
            "frames_censored": self.frames_censored,
        }

    def update(self, **fields) -> None:
        with self.changed:
            for k, v in fields.items():
                setattr(self, k, v)
            self.changed.notify_all()


def job_id(src: str) -> str:
    return hashlib.sha256(src.encode()).hexdigest()[:24]


class VideoJobs:
    """Starts, deduplicates and caches jobs. Thread-safe."""

    def __init__(self, cfg: Config, detector: Detector | None = None):
        self.cfg = cfg
        vcfg = cfg.video
        self.dir = Path(vcfg.cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._detector = detector
        self._detector_lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._slots = threading.Semaphore(vcfg.max_concurrent_jobs)

    @property
    def detector(self) -> Detector:
        with self._detector_lock:
            if self._detector is None:
                v = self.cfg.video
                self._detector = Detector(replace(self.cfg.model, path=v.model_path,
                                                  resolution=v.model_resolution, tiling=v.tiling))
            return self._detector

    def get(self, id_: str) -> Job | None:
        with self._lock:
            return self._jobs.get(id_)

    def active(self) -> int:
        with self._lock:
            return sum(j.state in ("queued", "running") for j in self._jobs.values())

    def start(self, src: str, referer: str | None = None, user_agent: str | None = None) -> Job:
        check_source(src, allow_private=self.cfg.video.allow_private_sources)
        id_ = job_id(src)
        with self._lock:
            job = self._jobs.get(id_)
            if job and job.state != "error":
                return job
            job = Job(id_, src, referer, user_agent, path=self.dir / f"{id_}.mp4")
            self._jobs[id_] = job
        threading.Thread(target=self._run, args=(job,), name=f"video-{id_[:8]}", daemon=True).start()
        return job

    def _run(self, job: Job) -> None:
        with self._slots:
            try:
                self._evict()
                job.update(state="running")
                censor_video(job, self.cfg, self.detector)
                job.update(state="done", finished=time.time())
                log.info("video done %s: %.0fs, %d frames censored", job.id, job.seconds_done,
                         job.frames_censored)
            except Exception as e:
                log.warning("video failed %s: %s", job.id, e)
                job.update(state="error", error=str(e) or type(e).__name__, finished=time.time())

    def _evict(self) -> None:
        """Delete the oldest finished outputs until the cache fits."""
        limit = self.cfg.video.cache_max_bytes
        with self._lock:
            done = sorted((j for j in self._jobs.values() if j.state in ("done", "error")),
                          key=lambda j: j.finished or 0)
            total = sum(j.path.stat().st_size for j in done if j.path and j.path.exists())
            for j in done:
                if total <= limit:
                    break
                if j.path and j.path.exists():
                    total -= j.path.stat().st_size
                    j.path.unlink()
                del self._jobs[j.id]


def check_source(src: str, allow_private: bool = False) -> None:
    """Refuse anything but public http(s) URLs, so pages can't use the proxy
    to reach the local network or the proxy itself."""
    parts = urlsplit(src)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise VideoError("source must be an http(s) URL")
    if allow_private:
        return
    try:
        infos = socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise VideoError(f"cannot resolve {parts.hostname}") from e
    for info in infos:
        addr = ipaddress.ip_address(info[4][0])
        if not addr.is_global:
            raise VideoError(f"{parts.hostname} is not a public address")


def _net_args(job: Job) -> list[str]:
    args = []
    if job.user_agent:
        args += ["-user_agent", job.user_agent]
    if job.referer:
        args += ["-headers", f"Referer: {job.referer}\r\n"]
    return args


def probe(job: Job, timeout: float = 30) -> Probe:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
         *_net_args(job), job.src],
        capture_output=True, timeout=timeout)
    if out.returncode != 0:
        raise VideoError(f"cannot read source: {out.stderr.decode(errors='replace').strip()[:300]}")
    info = json.loads(out.stdout)
    streams = info.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video" and s.get("width")
              and s.get("disposition", {}).get("attached_pic") != 1]
    if not videos:
        raise VideoError("no video stream")
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    # Highest-resolution video; the encoder scales it down to max_height.
    v = max(videos, key=lambda s: (s["height"], int(s.get("bit_rate") or s.get("tags", {}).get("variant_bitrate") or 0)))
    a = max(audios, key=lambda s: int(s.get("bit_rate") or 0), default=None)
    duration = info.get("format", {}).get("duration") or v.get("duration")
    return Probe(v["index"], v["width"], v["height"], _fps(v), float(duration) if duration else None,
                 a["index"] if a else None)


def _fps(stream: dict) -> float:
    for key in ("avg_frame_rate", "r_frame_rate"):
        num, _, den = (stream.get(key) or "0/0").partition("/")
        if den and float(den) and float(num):
            return float(num) / float(den)
    return 30.0


def output_size(p: Probe, max_height: int) -> tuple[int, int]:
    h = min(p.height, max_height)
    w = round(p.width * h / p.height)
    return w - w % 2, h - h % 2


def censor_video(job: Job, cfg: Config, detector: Detector) -> None:
    vcfg = cfg.video
    p = probe(job)
    if p.duration and p.duration > vcfg.max_duration_seconds:
        raise VideoError(f"video is {p.duration:.0f}s, over the {vcfg.max_duration_seconds}s limit")
    job.update(duration=p.duration)
    w, h = output_size(p, vcfg.max_height)
    fps = min(p.fps, vcfg.max_fps)
    step = max(1, round(fps / vcfg.sample_fps))
    frame_bytes = w * h * 3

    net = _net_args(job)
    decode_cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", *net, "-i", job.src,
                  "-map", f"0:{p.video_index}", "-vf", f"scale={w}:{h}", "-r", f"{fps:.6g}",
                  "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    encode_cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                  "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", f"{fps:.6g}",
                  "-i", "pipe:0"]
    if vcfg.audio and p.audio_index is not None:
        encode_cmd += [*net, "-i", job.src, "-map", "0:v", "-map", f"1:{p.audio_index}",
                       "-c:a", "aac", "-b:a", "128k", "-shortest"]
    else:
        encode_cmd += ["-map", "0:v"]
    encode_cmd += ["-c:v", "libx264", "-preset", vcfg.preset, "-crf", str(vcfg.crf),
                   "-pix_fmt", "yuv420p", "-g", str(max(1, round(fps * 2))),
                   "-movflags", "+frag_keyframe+empty_moov+default_base_moof",
                   "-f", "mp4", str(job.path)]

    with tempfile.TemporaryFile() as dec_err, tempfile.TemporaryFile() as enc_err:
        dec = subprocess.Popen(decode_cmd, stdout=subprocess.PIPE, stderr=dec_err)
        enc = subprocess.Popen(encode_cmd, stdin=subprocess.PIPE, stderr=enc_err)
        try:
            _pump(dec.stdout, enc.stdin, w, h, frame_bytes, fps, step, job, cfg, detector)
        finally:
            for proc in (dec, enc):
                if proc.stdin:
                    try:
                        proc.stdin.close()
                    except BrokenPipeError:
                        pass
            enc_rc, dec_rc = enc.wait(timeout=120), dec.wait(timeout=30)
        if dec_rc != 0:
            dec_err.seek(0)
            raise VideoError(f"decode failed: {dec_err.read().decode(errors='replace').strip()[:300]}")
        if enc_rc != 0:
            enc_err.seek(0)
            raise VideoError(f"encode failed: {enc_err.read().decode(errors='replace').strip()[:300]}")


def _pump(src, dst, w, h, frame_bytes, fps, step, job: Job, cfg: Config, detector: Detector) -> None:
    """Read raw frames, censor, write raw frames. Frames between samples wait
    in `pending` until the next sample's boxes are known."""
    pad = cfg.video.padding
    pending: list[np.ndarray] = []
    prev_boxes: list[Box] = []
    index = written = failures = 0

    def flush(frames: list[np.ndarray], boxes: list[Box]) -> None:
        nonlocal written
        for frame in frames:
            for box in boxes:
                apply_censor(frame, box, cfg.censor)
            dst.write(frame.tobytes())
        written += len(frames)
        job.update(seconds_done=written / fps,
                   frames_censored=job.frames_censored + (len(frames) if boxes else 0))

    while True:
        buf = src.read(frame_bytes)
        if len(buf) < frame_bytes:
            break
        frame = np.frombuffer(buf, np.uint8).reshape(h, w, 3).copy()
        if index % step:
            pending.append(frame)
        else:
            try:
                bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                boxes = [d.detection.box.padded(pad, w, h)
                         for d in decide(detector.detect(bgr), cfg.detection) if d.censor]
                failures = 0
            except Exception as e:
                failures += 1
                if failures >= 3:
                    raise VideoError(f"detection failing repeatedly: {e}") from e
                # Fail closed: this stretch is covered completely.
                boxes = [Box(0, 0, w, h)]
            flush(pending, prev_boxes + boxes)
            flush([frame], prev_boxes + boxes)
            pending, prev_boxes = [], boxes
        index += 1
    flush(pending, prev_boxes)
