"""Video censoring jobs: ffmpeg decode -> detect on sampled frames -> blur ->
ffmpeg encode to fixed-length fragmented MP4 segments, written to disk as
they are produced so playback can start before the job finishes.

ffmpeg reads plain files, HLS and DASH natively, so one pipeline covers all
three. Boxes found on a sampled frame are applied to every frame up to the
next sample, together with that sample's boxes, so motion between samples
stays covered (the same idea as GIF frame sampling).

Segments make seeking work while a job runs. Segment n always holds the
same frames, whichever run produced it. A run (one decoder and encoder)
starts at a segment and goes on until the end of the video, a segment that
is already done, or a viewer seeking somewhere it won't reach soon; then the
job starts a new run where it is needed. Each run's segments carry
timestamps from 0 and its own init segment, so the page's player is told
both with each segment.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

import cv2
import numpy as np

from .attribution import Scanner, Timeline, male_detection
from .config import Config
from .detection import Box, Detector, decide
from .imaging import apply_censor

log = logging.getLogger("censor_proxy.video")

HLS = re.compile(r"\.m3u8$", re.IGNORECASE)


class VideoError(RuntimeError):
    pass


class JobRefused(VideoError):
    """Over the queue cap or a page's rate limit; try again later."""


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
    # Sent to the source host, from the browser-set Origin header (never the
    # page-supplied URL, which any page could forge to anything).
    referer: str | None = None
    user_agent: str | None = None
    # The page that asked, as the page reported it; shown in the admin panel.
    page: str | None = None
    # queued | running | done | error
    state: str = "queued"
    error: str = ""
    duration: float | None = None
    # Seconds of video in finished segments.
    seconds_done: float = 0.0
    frames_censored: int = 0
    # Output layout, set once the source is probed: segment n holds frames
    # n * segment_frames onwards, segment_seconds long (the last may be
    # shorter). segments is None while the length is unknown.
    segment_frames: int = 0
    segment_seconds: float = 0.0
    segments: int | None = None
    audio: bool = False
    # The MSE type's codecs parameter, read from the first init segment.
    codecs: str | None = None
    # Finished segments, and for each the run that made it: (run number,
    # first segment). A run's segments use its init segment, and their
    # timestamps start at 0 from its first segment.
    done: set[int] = field(default_factory=set)
    run_of: dict[int, tuple[int, int]] = field(default_factory=dict)
    runs: int = 0
    # The segment the current run is producing (None between runs) and the
    # one it started at, and the segment to restart at, when a viewer
    # seeked somewhere the run won't reach soon.
    position: int | None = None
    run_first: int = 0
    restart_at: int | None = None
    # Segment requests so far. Only the newest one still waiting may seek:
    # a player that seeks again drops its fetch, but the server goes on
    # waiting on it and would pull the job back.
    requests: int = 0
    # Whether a run can open the source partway (-ss). ffmpeg 7.1's HLS
    # reader can't with fMP4 segments: it reads garbage and ends with no
    # frames. Such a source is decoded from its start and trimmed instead.
    seekable: bool = True
    # When the current run last showed progress (time.monotonic()): a frame
    # read, or a wait on the attribution scan.
    alive: float = 0.0
    # Video time judged to be in a male stretch (video_attribution).
    seconds_male: float = 0.0
    created: float = field(default_factory=time.time)
    # Last time a page asked about this job (status, stream, start), and
    # last time it said the video was wanted now (playing or being prepared
    # in a visible tab, not paused or in the background).
    last_seen: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    # Set to stop the job (page closed, or preempted); the pump checks it.
    cancel_reason: str = ""
    finished: float | None = None
    # Output directory: init-<run>.mp4 and seg-<n>.m4s.
    out_dir: Path | None = None
    changed: threading.Condition = field(default_factory=threading.Condition, repr=False)

    def status(self, ready_seconds: float) -> dict:
        with self.changed:
            first = 0
            while first in self.done:
                first += 1
            ready = self.state == "done" or (self.state == "running" and self.codecs is not None
                                             and first * self.segment_seconds >= ready_seconds)
            return {
                "id": self.id,
                "state": self.state,
                "ready": ready,
                "error": self.error,
                "duration": self.duration,
                "seconds_done": round(self.seconds_done, 1),
                "frames_censored": self.frames_censored,
                "seconds_male": round(self.seconds_male, 1),
                "segment_seconds": self.segment_seconds,
                "segments": self.segments,
                "codecs": self.codecs,
            }

    def segment_path(self, n: int) -> Path:
        return self.out_dir / f"seg-{n:05d}.m4s"

    def init_path(self, run: int) -> Path:
        return self.out_dir / f"init-{run}.mp4"

    def check(self, n: int) -> bool:
        """Whether segment n is finished, noticing one the encoder has just
        written (it renames each segment into place once complete)."""
        with self.changed:
            if n in self.done:
                return True
            if self.out_dir is None or n not in self.run_of or not self.segment_path(n).exists():
                return False
            self.done.add(n)
            self.seconds_done = len(self.done) * self.segment_seconds
            if self.duration:
                self.seconds_done = min(self.seconds_done, self.duration)
            if self.codecs is None:
                self.codecs = _codecs(self.init_path(self.run_of[n][0]), self.audio)
            self.changed.notify_all()
            return True

    def first_missing(self, after: int = 0) -> int | None:
        """The first unfinished segment from `after` on, else from the
        start; None when every segment is done."""
        if self.segments is None:
            return None if self.check(after) else after
        for n in [*range(after, self.segments), *range(0, min(after, self.segments))]:
            if not self.check(n):
                return n
        return None

    def seek(self, n: int, wait_segments: int) -> None:
        """A viewer needs segment n. Unless the current run will get there
        within wait_segments, restart the job there."""
        with self.changed:
            if n in self.done or self.segments is None or not 0 <= n < self.segments:
                return
            # Everything from the run's first segment to its position has been
            # fed to the encoder, which writes a segment out only once it
            # has the next one's first frame.
            p = self.position
            if p is not None and self.run_first <= n <= p + wait_segments:
                return
            if self.restart_at != n:
                log.debug("video %s: seek to segment %d (at %s)", self.id, n, p)
                self.restart_at = n
                self.changed.notify_all()

    def take_restart(self) -> int | None:
        with self.changed:
            n, self.restart_at = self.restart_at, None
            return n

    def touch(self, active: bool = False) -> None:
        self.last_seen = time.time()
        if active:
            self.last_active = self.last_seen

    def abandoned(self, after: float) -> bool:
        return time.time() - self.last_seen > after

    def idle(self, after: float) -> bool:
        return time.time() - self.last_active > after

    def cancel(self, reason: str) -> None:
        if self.state in ("queued", "running") and not self.cancel_reason:
            self.cancel_reason = reason
            log.info("video %s: %s", self.id, reason)

    def stop_reason(self, abandon_seconds: float) -> str:
        if self.cancel_reason:
            return self.cancel_reason
        return "cancelled: nobody is watching" if self.abandoned(abandon_seconds) else ""

    def update(self, **fields) -> None:
        with self.changed:
            for k, v in fields.items():
                setattr(self, k, v)
            self.changed.notify_all()


# Output directories, and the single files earlier versions wrote.
_OUTPUT_NAME = re.compile(r"[0-9a-f]{24}-\d+(\.mp4)?")


class VideoJobs:
    """Starts, deduplicates and caches jobs. Thread-safe."""

    def __init__(self, cfg: Config, detector: Detector | None = None):
        self.cfg = cfg
        vcfg = cfg.video
        self.dir = Path(vcfg.cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        # Outputs from an earlier run aren't tracked, so eviction would never
        # see them. Only our own file names, in case cache_dir is shared.
        for f in self.dir.iterdir():
            if _OUTPUT_NAME.fullmatch(f.name):
                _remove(f)
        self._detector = detector
        self._attribution_detector: Detector | None = None
        self._detector_lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        # (origin, src) -> job id. Ids are random, not derived from the
        # source: a page that knows a video URL could otherwise poll the
        # status of a job another site started and learn what was watched.
        # One job per origin for the same reason (a cache hit would tell).
        self._ids: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()
        # Running jobs, up to video.max_concurrent_jobs as it is now: a
        # lower limit lets running jobs finish and starts no more until under it.
        self._running = 0
        self._slot_freed = threading.Condition(self._lock)
        self._attempts = 0
        self._starts: dict[str, deque[float]] = {}  # origin -> recent start times

    @property
    def detector(self) -> Detector:
        with self._detector_lock:
            if self._detector is None:
                v = self.cfg.video
                self._detector = Detector(replace(self.cfg.model, path=v.model_path,
                                                  resolution=v.model_resolution, tiling=v.tiling))
            return self._detector

    @property
    def attribution_detector(self) -> Detector | None:
        """The video_attribution model, or None while attribution is off."""
        a = self.cfg.video_attribution
        if not a.enabled:
            return None
        with self._detector_lock:
            if self._attribution_detector is None:
                self._attribution_detector = Detector(replace(
                    self.cfg.model, path=a.model_path, resolution=a.model_resolution, tiling=a.tiling))
            return self._attribution_detector

    def get(self, id_: str) -> Job | None:
        with self._lock:
            return self._jobs.get(id_)

    def list(self) -> list[Job]:
        """Every job this run has seen, newest first."""
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)

    def reconfigure(self, cfg: Config) -> None:
        """Use a new config for later jobs. The cache directory is fixed at
        startup."""
        with self._detector_lock:
            old, new = self.cfg, cfg
            if (old.model, old.video.model_path, old.video.model_resolution, old.video.tiling) != \
                    (new.model, new.video.model_path, new.video.model_resolution, new.video.tiling):
                self._detector = None
            if (old.model, old.video_attribution.model_path, old.video_attribution.model_resolution,
                    old.video_attribution.tiling) != \
                    (new.model, new.video_attribution.model_path,
                     new.video_attribution.model_resolution, new.video_attribution.tiling):
                self._attribution_detector = None
            self.cfg = cfg
        with self._lock:
            self._slot_freed.notify_all()  # a higher limit starts waiting jobs now

    def active(self) -> int:
        with self._lock:
            return sum(j.state in ("queued", "running") for j in self._jobs.values())

    def start(self, src: str, origin: str | None = None, user_agent: str | None = None,
              page: str | None = None) -> Job:
        """Start (or reuse) the job for `src`. `origin` is the requesting
        page's Origin header; it becomes the Referer and keys the rate limit."""
        v = self.cfg.video
        check_source(src, allow_private=v.allow_private_sources)
        key = (origin or "", src)
        with self._lock:
            self._count_start(origin or "")
            job = self._jobs.get(self._ids.get(key, ""))
            if job and job.state != "error" and not job.cancel_reason:
                job.touch(active=True)
                return job
            queued = sum(j.state == "queued" and not j.cancel_reason for j in self._jobs.values())
            if v.max_queued_jobs and queued >= v.max_queued_jobs:
                raise JobRefused(f"{queued} videos are already waiting; try again later")
            # Own directory per attempt: a cancelled run may still be shutting down.
            self._attempts += 1
            id_ = self._ids[key] = secrets.token_hex(12)
            job = Job(id_, src, origin + "/" if origin else None, user_agent, page=page,
                      out_dir=self.dir / f"{id_}-{self._attempts}")
            self._jobs[id_] = job
        threading.Thread(target=self._run, args=(job,), name=f"video-{id_[:8]}", daemon=True).start()
        return job

    def _count_start(self, origin: str) -> None:
        """Record a start for `origin`, refusing it over max_starts_per_minute.
        Called with the lock held."""
        limit = self.cfg.video.max_starts_per_minute
        if not limit:
            return
        now = time.time()
        recent = self._starts.setdefault(origin, deque())
        while recent and now - recent[0] > 60:
            recent.popleft()
        if len(recent) >= limit:
            raise JobRefused("too many video requests from this page; try again in a minute")
        recent.append(now)
        if len(self._starts) > 1000:  # forget origins that have gone quiet
            for key in [k for k, d in self._starts.items() if not d or now - d[-1] > 60]:
                del self._starts[key]

    def cancel(self, id_: str, reason: str) -> None:
        job = self.get(id_)
        if job:
            job.cancel(reason)

    def _wait_for_slot(self, job: Job) -> bool:
        """Wait for a free slot, taking one from a running job whose video
        is paused or in a background tab. False if this job was cancelled
        or abandoned while it waited."""
        while not self._take_slot():
            v = self.cfg.video
            if job.stop_reason(v.abandon_seconds):
                return False
            with self._lock:
                idle = [j for j in self._jobs.values() if j.state == "running"
                        and not j.cancel_reason and j.idle(v.idle_seconds)]
            if idle and not job.idle(v.idle_seconds):  # only for a video someone is waiting on
                min(idle, key=lambda j: j.last_active).cancel(
                    "stopped: paused or in the background while another video waited")
        return True

    def _take_slot(self) -> bool:
        """Take a slot, waiting up to half a second for one to come free."""
        with self._lock:
            if self._running >= self.cfg.video.max_concurrent_jobs:
                self._slot_freed.wait(timeout=0.5)
            if self._running >= self.cfg.video.max_concurrent_jobs:
                return False
            self._running += 1
            return True

    def _release_slot(self) -> None:
        with self._lock:
            self._running -= 1
            self._slot_freed.notify()

    def _run(self, job: Job) -> None:
        if not self._wait_for_slot(job):
            self._fail(job, job.stop_reason(self.cfg.video.abandon_seconds))
            return
        try:
            try:
                if reason := job.stop_reason(self.cfg.video.abandon_seconds):
                    raise VideoError(reason)
                self._evict()
                job.update(state="running")
                censor_video(job, self.cfg, self.detector, self.attribution_detector)
                job.update(state="done", finished=time.time())
                log.info("video done %s: %.0fs, %d frames censored, %.0fs male", job.id,
                         job.seconds_done, job.frames_censored, job.seconds_male)
            except Exception as e:
                self._fail(job, str(e) or type(e).__name__)
        finally:
            self._release_slot()

    def _fail(self, job: Job, error: str) -> None:
        log.warning("video failed %s: %s", job.id, error)
        job.update(state="error", error=error, finished=time.time())
        # Failed output is never served; a player keeps what it already has.
        if job.out_dir:
            _remove(job.out_dir)

    def _evict(self) -> None:
        """Delete the oldest finished outputs until the cache fits."""
        limit = self.cfg.video.cache_max_bytes
        with self._lock:
            done = sorted((j for j in self._jobs.values() if j.state in ("done", "error")),
                          key=lambda j: j.finished or 0)
            sizes = {j.id: _size(j.out_dir) for j in done}
            total = sum(sizes.values())
            for j in done:
                if total <= limit:
                    break
                if j.out_dir:
                    total -= sizes[j.id]
                    _remove(j.out_dir)
                del self._jobs[j.id]


def _size(path: Path | None) -> int:
    if not path or not path.is_dir():
        return 0
    return sum(f.stat().st_size for f in path.iterdir() if f.is_file())


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


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


def _net_args(job: Job, protocols: tuple[str, ...] = ()) -> list[str]:
    # Only the configured protocols, for the source and everything it pulls
    # in (HLS segments and keys): a playlist can't point ffmpeg at file: or
    # other protocols. Must precede -i.
    args = ["-protocol_whitelist", ",".join(protocols)] if protocols else []
    # Some CDNs (Pornhub's, behind Cloudflare) answer 410 to requests that
    # don't look like a browser's. User-Agent goes in -headers because
    # -user_agent puts it before Host, which browsers never do.
    headers = ""
    if job.user_agent:
        headers += f"User-Agent: {job.user_agent}\r\n"
    if job.referer:
        headers += f"Referer: {job.referer}\r\n"
    if headers:
        args += ["-headers", headers]
    if HLS.search(urlsplit(job.src).path):
        # No open-ended "Range: bytes=0-" (the same CDNs refuse it); the
        # demuxer's own option covers its playlist and segment requests.
        # Playlists don't need seeking; files can (moov at the end).
        args += ["-seekable", "0", "-http_seekable", "0"]
    return args


def probe(job: Job, protocols: tuple[str, ...] = (), timeout: float = 30) -> Probe:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
         *_net_args(job, protocols), job.src],
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




def _codecs(init: Path, audio: bool) -> str | None:
    """The MSE codecs parameter for an init segment: avc1.PPCCLL from its
    avcC box, and AAC-LC when there is audio."""
    try:
        data = init.read_bytes()
    except OSError:
        return None
    i = data.find(b"avcC")
    if i < 0 or len(data) < i + 8:
        return None
    video = f"avc1.{data[i + 5]:02x}{data[i + 6]:02x}{data[i + 7]:02x}"
    return f"{video},mp4a.40.2" if audio else video


class _Seek(Exception):
    """A viewer seeked where the current run won't get to soon."""


# How long a seek or cancel may wait for the pump to notice it before the
# watchdog kills the run's ffmpegs. The pump checks at every sample, so
# only a pump blocked on a pipe takes this long.
INTERRUPT_GRACE = 2.0


class _Watchdog:
    """Stops a run the pump can't stop itself because it is blocked reading
    the decoder or writing the encoder (a stalled source read holds both): it
    kills the ffmpegs when a seek or cancel goes unanswered, or when no frame
    has come for stall_seconds, and then asks for a restart at the segment
    the run was on."""

    def __init__(self, job: Job, procs, cfg: Config):
        self.job, self.procs, self.cfg = job, procs, cfg
        self.fired = False
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._watch, name=f"watch-{job.id[:8]}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._done.set()
        self._thread.join()

    def _watch(self) -> None:
        job, v = self.job, self.cfg.video
        pending_since = None
        while not self._done.wait(0.25):
            now = time.monotonic()
            if job.restart_at is not None or job.stop_reason(v.abandon_seconds):
                pending_since = pending_since or now
                if now - pending_since < INTERRUPT_GRACE:
                    continue
                log.info("video %s: run blocked; stopping it for a seek or cancel", job.id)
            elif v.stall_seconds and now - job.alive > v.stall_seconds:
                with job.changed:
                    if job.restart_at is None and job.position is not None:
                        job.restart_at = job.position
                log.warning("video %s: no frames for %.0fs; restarting at segment %s",
                            job.id, now - job.alive, job.position)
            else:
                pending_since = None
                continue
            self.fired = True
            for proc in self.procs:
                proc.kill()
            return


def censor_video(job: Job, cfg: Config, detector: Detector,
                 attribution_detector: Detector | None = None) -> None:
    """Censor job.src into segments in job.out_dir, one run after another
    until every segment is done. With an attribution detector (and
    video_attribution on), each run also decodes a second time at its
    sample rate to decide which stretches are male; see attribution.py."""
    vcfg = cfg.video
    p = probe(job, vcfg.source_protocols)
    if p.duration and p.duration > vcfg.max_duration_seconds:
        raise VideoError(f"video is {p.duration:.0f}s, over the {vcfg.max_duration_seconds}s limit")
    w, h = output_size(p, vcfg.max_height)
    fps = min(p.fps, vcfg.max_fps)
    step = max(1, round(fps / vcfg.sample_fps))
    # A whole number of samples per segment, so every segment starts on a
    # sampled frame and a run can stop there with its boxes settled.
    seg = step * max(1, round(vcfg.segment_seconds * fps / step))
    job.update(duration=p.duration, segment_frames=seg, segment_seconds=seg / fps,
               segments=max(1, math.ceil(p.duration * fps / seg - 1e-6)) if p.duration else None,
               audio=vcfg.audio and p.audio_index is not None)
    job.out_dir.mkdir(parents=True, exist_ok=True)
    start = job.take_restart() or 0
    while start is not None:
        start = _censor_run(job, start, p, w, h, fps, step, cfg, detector, attribution_detector)


def _censor_run(job: Job, first: int, p: Probe, w: int, h: int, fps: float, step: int,
                cfg: Config, detector: Detector, attribution_detector: Detector | None) -> int | None:
    """One run, from segment `first`. Returns the segment the next run
    starts at, or None when every segment is done."""
    vcfg = cfg.video
    seg = job.segment_frames
    t0 = first * job.segment_seconds
    with job.changed:
        job.runs += 1
        run = job.runs
        # Claimed now, not at the first frame: opening the source at t0 can
        # take seconds, and a request for `first` meanwhile (or one made
        # between runs) must not restart the run before it starts.
        job.position, job.run_first = first, first
        if job.restart_at == first:
            job.restart_at = None
        # Opening the source counts against stall_seconds, and so does
        # decoding up to t0 on a source that can't be opened there.
        skip = 0.0 if job.seekable else t0 / vcfg.skip_speed
        job.alive = time.monotonic() + skip
    net = _net_args(job, vcfg.source_protocols)
    seek = ["-ss", f"{t0:.6f}"] if first and job.seekable else []
    # Otherwise frames before t0 are decoded and dropped. Half a frame early,
    # so rounding never drops the one at t0.
    trim = f"trim=start={t0 - 0.5 / fps:.6f},setpts=PTS-STARTPTS," if first and not seek else ""
    decode_cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", *net, *seek, "-i", job.src,
                  "-map", f"0:{p.video_index}", "-vf", f"{trim}scale={w}:{h}", "-r", f"{fps:.6g}",
                  "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
    encode_cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", "-y",
                  "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", f"{fps:.6g}",
                  "-i", "pipe:0"]
    if job.audio:
        encode_cmd += [*net, *seek, "-i", job.src, "-map", "0:v", "-map", f"1:{p.audio_index}",
                       "-c:a", "aac", "-b:a", "128k", "-shortest"]
        if trim:
            encode_cmd += ["-af", f"atrim=start={t0:.6f},asetpts=PTS-STARTPTS"]
    else:
        encode_cmd += ["-map", "0:v"]
    encode_cmd += ["-c:v", "libx264", "-preset", vcfg.preset, "-crf", str(vcfg.crf),
                   "-pix_fmt", "yuv420p",
                   # Keyframes exactly at segment starts and nowhere else, so
                   # every run cuts segment n at the same frame. The muxer
                   # cuts at the first keyframe past each multiple of hls_time.
                   "-g", str(seg), "-keyint_min", str(seg), "-sc_threshold", "0",
                   "-force_key_frames", f"expr:eq(mod(n,{seg}),0)",
                   "-f", "hls", "-hls_segment_type", "fmp4",
                   "-hls_time", f"{(seg - 0.5) / fps:.6f}", "-hls_list_size", "0",
                   # Each segment is renamed into place once complete.
                   "-hls_flags", "temp_file", "-start_number", str(first),
                   "-hls_fmp4_init_filename", job.init_path(run).name,
                   "-hls_segment_filename", str(job.out_dir / "seg-%05d.m4s"),
                   str(job.out_dir / f"run-{run}.m3u8")]

    scanner = None
    acfg = cfg.video_attribution
    if attribution_detector and acfg.enabled:
        # From history_seconds back, so the run's first moments have their window.
        scan_t0 = max(0.0, t0 - acfg.history_seconds)
        scan_seek = ["-ss", f"{scan_t0:.6f}"] if scan_t0 and job.seekable else []
        scan_trim = f"trim=start={scan_t0:.6f},setpts=PTS-STARTPTS," if scan_t0 and not scan_seek else ""
        scan_cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", *net, *scan_seek, "-i", job.src,
                    "-map", f"0:{p.video_index}",
                    "-vf", f"{scan_trim}fps={acfg.sample_fps:.6g},scale={w}:{h}",
                    "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
        scanner = Scanner(scan_cmd, w, h, acfg, attribution_detector,
                          name=f"attrib-{job.id[:8]}", t0=scan_t0)

    if first:
        log.info("video %s: run %d from %.1fs", job.id, run, t0)
    stopped, frames = "seek", 0
    with tempfile.TemporaryFile() as dec_err, tempfile.TemporaryFile() as enc_err:
        timeline = scanner.start() if scanner else None
        dec = subprocess.Popen(decode_cmd, stdout=subprocess.PIPE, stderr=dec_err)
        enc = subprocess.Popen(encode_cmd, stdin=subprocess.PIPE, stderr=enc_err)
        watchdog = _Watchdog(job, (dec, enc), cfg)
        try:
            stopped, frames = _pump(dec.stdout, enc.stdin, w, h, w * h * 3, fps, step, job, cfg,
                                    detector, timeline, first=first, segment_frames=seg, run=run)
        except _Seek:
            # The segment in progress is lost; its temporary file is removed below.
            for proc in (dec, enc):
                proc.kill()
        except BaseException as e:
            # Cancelled or failed: the encoder would otherwise keep reading
            # the audio input from the network before it noticed.
            for proc in (dec, enc):
                proc.kill()
            if not (watchdog.fired and isinstance(e, Exception)):  # a pipe closing under it
                raise
        finally:
            watchdog.stop()
            if scanner:
                scanner.stop()
            if stopped == "reached":
                dec.kill()  # the rest of the source isn't needed
            dec.stdout.close()
            for proc in (dec, enc):
                if proc.stdin:
                    try:
                        proc.stdin.close()
                    except BrokenPipeError:
                        pass
            enc_rc, dec_rc = enc.wait(timeout=120), dec.wait(timeout=30)
            job.update(position=None)
        if watchdog.fired:
            # However the pump ended once its pipes were killed, it was stopped.
            if reason := job.stop_reason(vcfg.abandon_seconds):
                raise VideoError(reason)
            stopped = "seek"
        if stopped == "seek":
            for f in job.out_dir.glob("*.tmp"):
                f.unlink(missing_ok=True)
        else:
            if stopped == "end" and dec_rc != 0:
                dec_err.seek(0)
                raise VideoError(f"decode failed: {dec_err.read().decode(errors='replace').strip()[:300]}")
            if enc_rc != 0:
                enc_err.seek(0)
                raise VideoError(f"encode failed: {enc_err.read().decode(errors='replace').strip()[:300]}")

    last = first + math.ceil(frames / seg)  # one past the last segment this run fed
    for n in range(first, last):
        job.check(n)  # the encoder wrote the last ones as it finished
    # The probe's duration is only an estimate, so the real end may come up
    # to a segment before it; earlier means the read failed.
    short = stopped == "end" and job.segments is not None and last < job.segments - 1
    if short and not frames and seek:
        log.warning("video %s: no frames after seeking to %.1fs; decoding from the start instead",
                    job.id, t0)
        job.update(seekable=False)
        return first
    if short:
        raise VideoError(f"the source ended at {last * job.segment_seconds:.0f}s "
                         f"of {job.duration:.0f}s")
    if stopped == "end":
        if not last:
            raise VideoError("the source has no video frames")
        if frames and not job.check(first):
            raise VideoError("the encoder wrote no segments")
        job.update(segments=last)  # the real length
    restart = job.take_restart()
    if restart is not None and not job.check(restart):
        return restart
    return job.first_missing(last if stopped != "seek" else first)


def _pump(src, dst, w, h, frame_bytes, fps, step, job: Job, cfg: Config, detector: Detector,
          timeline: Timeline | None = None, first: int = 0, segment_frames: int = 0,
          run: int = 0) -> tuple[str, int]:
    """Read raw frames, censor, write raw frames. Frames between samples wait
    in `pending` until the next sample's boxes are known. With a timeline,
    each sample first waits for the attribution scan to get far enough ahead,
    and male stretches use the male detection config.

    With segment_frames, frame 0 starts segment `first`, and at each segment
    start the pump stops if that segment is already done, or else claims it
    for `run`. Returns why it stopped ("end" or "reached") and the frames
    written; raises _Seek when a viewer seeked elsewhere."""
    pad = cfg.video.padding
    acfg = cfg.video_attribution
    male_cfg = male_detection(cfg.detection, acfg)
    t0 = first * segment_frames / fps
    pending: list[np.ndarray] = []
    prev_boxes: list[Box] = []
    unconfirmed: list[int] = []  # segments fed to the encoder, not yet on disk
    index = written = failures = 0
    was_male = scan_failed = False

    def attributed_male(t: float) -> bool:
        nonlocal was_male, scan_failed
        while not timeline.wait(t + acfg.lookahead_seconds, 0.5):
            job.alive = time.monotonic()  # waiting on the scan isn't a stall
            if reason := job.stop_reason(cfg.video.abandon_seconds):
                raise VideoError(reason)
            if job.restart_at is not None:
                raise _Seek()
        if timeline.failed and not scan_failed:
            scan_failed = True
            log.warning("video %s: attribution off from %.1fs: %s", job.id, t, timeline.failed)
        male = timeline.male_at(t)
        if male != was_male:
            log.debug("video %s: %s from %.1fs", job.id, "male stretch" if male else "unattributed", t)
            was_male = male
        return male

    def segment_start() -> bool:
        """At the first frame of a segment: claim it, or False to stop
        because it is already done."""
        m = first + index // segment_frames
        if job.check(m):
            return False
        if index:
            unconfirmed.append(m - 1)
        with job.changed:
            job.run_of[m] = (run, first)
            job.position, job.run_first = m, first
        return True

    def flush(frames: list[np.ndarray], boxes: list[Box]) -> None:
        nonlocal written
        for frame in frames:
            for box in boxes:
                apply_censor(frame, box, cfg.censor)
            dst.write(frame.tobytes())
        written += len(frames)
        job.update(frames_censored=job.frames_censored + (len(frames) if boxes else 0))

    while True:
        buf = src.read(frame_bytes)
        if len(buf) < frame_bytes:
            break
        job.alive = time.monotonic()
        frame = np.frombuffer(buf, np.uint8).reshape(h, w, 3).copy()
        if index % step == 0:
            if reason := job.stop_reason(cfg.video.abandon_seconds):
                raise VideoError(reason)
            if job.restart_at is not None:
                raise _Seek()
        if index % step:
            pending.append(frame)
        else:
            detection = cfg.detection
            if timeline and attributed_male(t0 + index / fps):
                detection = male_cfg
                job.update(seconds_male=job.seconds_male + step / fps)
            try:
                bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                boxes = [d.detection.box.padded(pad, w, h)
                         for d in decide(detector.detect(bgr), detection) if d.censor]
                failures = 0
            except Exception as e:
                failures += 1
                if failures >= 3:
                    raise VideoError(f"detection failing repeatedly: {e}") from e
                # Fail closed: this stretch is covered completely.
                boxes = [Box(0, 0, w, h)]
            flush(pending, prev_boxes + boxes)
            if segment_frames and index % segment_frames == 0 and not segment_start():
                return "reached", written
            flush([frame], prev_boxes + boxes)
            pending, prev_boxes = [], boxes
            unconfirmed[:] = [m for m in unconfirmed if not job.check(m)]
        index += 1
    flush(pending, prev_boxes)
    return "end", written
