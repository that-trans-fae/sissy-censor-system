"""censor_bytes(): decode, detect, censor regions, re-encode.

Every caller (image responses, and later data URIs in text bodies) goes
through Censor.censor_bytes so behaviour and thresholds are identical.
"""

from __future__ import annotations

import hashlib
import io
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image, ImageOps, ImageSequence

from .config import CensorConfig, Config
from .detection import Box, Decision, Detector, decide

SUPPORTED_FORMATS = {"JPEG", "PNG", "WEBP", "GIF", "AVIF"}
MIME_FORMATS = {
    "image/jpeg": "JPEG",
    "image/jpg": "JPEG",
    "image/pjpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
    "image/gif": "GIF",
    "image/avif": "AVIF",
}


def sniff(data: bytes) -> str | None:
    """The MIME type of a supported image format, from its leading bytes; for
    responses whose Content-Type doesn't say."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[4:8] == b"ftyp":
        # Major brand, then (after the minor version) compatible brands: AVIF
        # may be "mif1" major with "avif" compatible.
        end = min(int.from_bytes(data[:4], "big"), 64)
        brands = {data[8:12]} | {data[i:i + 4] for i in range(16, end - 3, 4)}
        if brands & {b"avif", b"avis"}:
            return "image/avif"
    return None


@dataclass
class ScanResult:
    data: bytes
    # clean | censored | fail_closed | skipped | error, or unscanned (a
    # response requested as an image that never reached the censor; logged
    # at debug)
    action: str
    reason: str = ""
    format: str | None = None
    frames: int = 0
    # (frame index, decisions) for every frame that went through the model
    frame_decisions: list[tuple[int, list[Decision]]] = field(default_factory=list)
    cached: bool = False
    elapsed_ms: float = 0.0

    @property
    def changed(self) -> bool:
        return self.action in ("censored", "fail_closed")

    @property
    def censored_decisions(self) -> list[Decision]:
        return [d for _, ds in self.frame_decisions for d in ds if d.censor]


@dataclass
class _Decoded:
    format: str
    frames: list[np.ndarray]  # RGB or RGBA uint8
    has_alpha: bool
    durations: list[int]
    loop: int | None
    info: dict  # icc_profile, exif, lossless (WebP)


class Censor:
    def __init__(self, cfg: Config, detector: Detector | None = None):
        self.cfg = cfg
        self.detector = detector or Detector(cfg.model)
        self._cache: OrderedDict[str, ScanResult] = OrderedDict()
        self._cache_lock = threading.Lock()

    # -- public -----------------------------------------------------------

    def cached(self) -> int:
        with self._cache_lock:
            return len(self._cache)

    def censor_bytes(self, data: bytes, mime: str | None = None,
                     deadline: float | None = None) -> ScanResult:
        """Censor female explicit regions in an encoded image.

        Returns the original bytes untouched unless something was censored.
        Raises on detection failure; callers decide whether to fail closed.
        Past `deadline` (a time.perf_counter() value) it raises TimeoutError
        between frames, so a timed-out animation gives its worker back
        instead of scanning on after its caller has given up.
        """
        start = time.perf_counter()
        result = self._scan(data, mime, deadline)
        result.elapsed_ms = (time.perf_counter() - start) * 1000
        return result

    def precheck(self, data: bytes, mime: str | None = None) -> ScanResult | None:
        """censor_bytes' result for an image that needs no detection (too
        small, not a supported image, or already cached), from its header and
        hash alone; None if it needs the full scan. Callers run this before
        queueing for a detection worker, so such images never wait behind
        slow ones (animated GIFs take seconds each)."""
        start = time.perf_counter()
        quick = self._quick(data, mime)
        if not isinstance(quick, ScanResult):
            return None
        quick.elapsed_ms = (time.perf_counter() - start) * 1000
        return quick

    def fail_closed(self, data: bytes, reason: str, first_frame_only: bool = False) -> ScanResult:
        """Censor the whole image (every frame). Used when detection fails.
        With first_frame_only, an animation becomes its first frame, fully
        censored; for images too big to decode in full."""
        start = time.perf_counter()
        try:
            decoded = _decode(data, first_frame_only)
        except Exception as e:  # undecodable: nothing we can blur
            return ScanResult(data, "error", f"{reason}; undecodable, delivered unmodified: {e}")
        for frame in decoded.frames:
            h, w = frame.shape[:2]
            apply_censor(frame, Box(0, 0, w, h), self.cfg.censor)
        out = _encode(decoded, self.cfg.censor)
        return ScanResult(out, "fail_closed", reason, decoded.format, len(decoded.frames),
                          elapsed_ms=(time.perf_counter() - start) * 1000)

    # -- internals --------------------------------------------------------

    def _quick(self, data: bytes, mime: str | None) -> ScanResult | tuple[str, str | None]:
        """The checks that need no decoding: a result if they settle it, else
        (format, cache key). Reads only the header; the frame count can mean
        walking a whole GIF, so it's left to _scan. Oversized images (they
        fail closed, which is never cached) aren't hashed."""
        icfg = self.cfg.images
        if len(data) < icfg.size_floor_bytes:
            return ScanResult(data, "skipped", "below size floor")
        try:
            with Image.open(io.BytesIO(data)) as probe:
                fmt, size = probe.format, probe.size
        except Exception:
            return ScanResult(data, "skipped", f"not a decodable image ({mime})")
        if fmt not in SUPPORTED_FORMATS:
            return ScanResult(data, "skipped", f"unsupported format {fmt}", fmt)
        if min(size) < icfg.min_side_px:
            return ScanResult(data, "skipped", "below min_side_px", fmt)
        if len(data) > icfg.max_bytes:
            return fmt, None
        key = hashlib.sha256(data).hexdigest()
        if (hit := self._cache_get(key)) is not None:
            return hit
        return fmt, key

    def _scan(self, data: bytes, mime: str | None, deadline: float | None) -> ScanResult:
        icfg = self.cfg.images
        quick = self._quick(data, mime)
        if isinstance(quick, ScanResult):
            return quick
        fmt, key = quick
        with Image.open(io.BytesIO(data)) as probe:
            size, n_frames = probe.size, getattr(probe, "n_frames", 1)
        # Too big to scan: deliver the first frame fully censored, without
        # decoding the rest.
        decoded_bytes = size[0] * size[1] * 4 * n_frames
        if len(data) > icfg.max_bytes:
            return self.fail_closed(data, f"{len(data)} bytes exceeds max_bytes", first_frame_only=True)
        if decoded_bytes > icfg.max_decoded_bytes:
            return self.fail_closed(data, f"{decoded_bytes} decoded bytes exceeds max_decoded_bytes",
                                    first_frame_only=True)
        if n_frames > icfg.max_frames:
            return self.fail_closed(data, f"{n_frames} frames exceeds max_frames", first_frame_only=True)

        decoded = _decode(data)
        frame_decisions = self._detect_frames(decoded.frames, deadline)
        boxes_per_frame = _boxes_per_frame(len(decoded.frames), frame_decisions)

        if not any(boxes_per_frame):
            result = ScanResult(data, "clean", "", fmt, len(decoded.frames), frame_decisions)
        else:
            for frame, boxes in zip(decoded.frames, boxes_per_frame):
                h, w = frame.shape[:2]
                for box in boxes:
                    apply_censor(frame, box.padded(self.cfg.censor.padding, w, h), self.cfg.censor)
            result = ScanResult(_encode(decoded, self.cfg.censor), "censored", "", fmt,
                                len(decoded.frames), frame_decisions)
        self._cache_put(key, result)
        return result

    def _detect_frames(self, frames: list[np.ndarray],
                       deadline: float | None = None) -> list[tuple[int, list[Decision]]]:
        sampled = _sample_indices(frames, self.cfg.images)
        seen: dict[bytes, list[Decision]] = {}
        out = []
        for i in sampled:
            frame = frames[i]
            digest = hashlib.blake2b(frame.tobytes(), digest_size=16).digest()
            if digest not in seen:
                if deadline is not None and time.perf_counter() > deadline:
                    raise TimeoutError(f"deadline passed after {len(seen)} of {len(sampled)} frames")
                bgr = cv2.cvtColor(np.ascontiguousarray(frame[..., :3]), cv2.COLOR_RGB2BGR)
                seen[digest] = decide(self.detector.detect(bgr), self.cfg.detection)
            out.append((i, seen[digest]))
        return out

    def _cache_get(self, key: str) -> ScanResult | None:
        if self.cfg.cache.max_entries <= 0:
            return None
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit is None:
                return None
            self._cache.move_to_end(key)
        return ScanResult(**{**hit.__dict__, "cached": True})

    def _cache_put(self, key: str, result: ScanResult) -> None:
        if self.cfg.cache.max_entries <= 0:
            return
        with self._cache_lock:
            self._cache[key] = result
            while len(self._cache) > self.cfg.cache.max_entries:
                self._cache.popitem(last=False)


def is_animated(data: bytes) -> bool:
    """Whether an image has more than one frame, from its header (a GIF is
    read only as far as its second frame)."""
    try:
        with Image.open(io.BytesIO(data)) as probe:
            return bool(getattr(probe, "is_animated", False))
    except Exception:
        return False


def _sample_indices(frames: list[np.ndarray], icfg) -> list[int]:
    """Frames to run detection on: every `frame_step`th, the last, and every
    scene cut (a frame unlike the one before it). Skipped frames borrow their
    neighbours' boxes, which only fits when the picture moves between them; a
    slideshow GIF changes picture every frame. At most `max_detect_frames`."""
    n, budget = len(frames), icfg.max_detect_frames
    cuts = []
    if icfg.scene_cut_threshold > 0 and n > 1:
        thumbs = [_thumb(f) for f in frames]
        cuts = [i for i in range(1, n)
                if np.abs(thumbs[i] - thumbs[i - 1]).mean() > icfg.scene_cut_threshold]
    step = max(icfg.frame_step, math.ceil(n / max(1, budget - len(cuts))))
    sampled = sorted(set(range(0, n, step)) | {n - 1} | set(cuts))
    if len(sampled) > budget:  # more cuts than the budget: spread it evenly
        sampled = sorted({sampled[round(k * (len(sampled) - 1) / (budget - 1))] for k in range(budget)})
    return sampled


def _thumb(frame: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(np.ascontiguousarray(frame[..., :3]), cv2.COLOR_RGB2GRAY)
    return cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.int16)


def _boxes_per_frame(n: int, frame_decisions: list[tuple[int, list[Decision]]]) -> list[list[Box]]:
    """Censor boxes for every frame. Unsampled frames get the union of the
    sampled frames on either side, so motion between samples stays covered."""
    sampled = {i: [d.detection.box for d in ds if d.censor] for i, ds in frame_decisions}
    indices = sorted(sampled)
    out = []
    for i in range(n):
        if i in sampled:
            out.append(sampled[i])
            continue
        prev = max(j for j in indices if j < i)
        nxt = min(j for j in indices if j > i)
        out.append(sampled[prev] + sampled[nxt])
    return out


def _decode(data: bytes, first_frame_only: bool = False) -> _Decoded:
    im = Image.open(io.BytesIO(data))
    fmt = im.format
    info = {"icc_profile": im.info.get("icc_profile")}
    if fmt == "WEBP":
        info["lossless"] = b"VP8L" in data[:64]
    n_frames = 1 if first_frame_only else getattr(im, "n_frames", 1)

    if n_frames == 1:
        oriented = ImageOps.exif_transpose(im)
        exif = oriented.getexif()
        info["exif"] = exif.tobytes() if len(exif) else None
        has_alpha = _has_alpha(oriented)
        frame = np.array(oriented.convert("RGBA" if has_alpha else "RGB"))
        return _Decoded(fmt, [frame], has_alpha, [], None, info)

    frames, durations = [], []
    for f in ImageSequence.Iterator(im):
        frames.append(np.array(f.convert("RGBA")))
        durations.append(int(f.info.get("duration", 100)))
    return _Decoded(fmt, frames, True, durations, im.info.get("loop", 0), info)


def _has_alpha(im: Image.Image) -> bool:
    return im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)


def _encode(d: _Decoded, cfg: CensorConfig) -> bytes:
    buf = io.BytesIO()
    images = [Image.fromarray(f) for f in d.frames]
    first = images[0]
    extra = {k: v for k, v in (("icc_profile", d.info.get("icc_profile")),
                                ("exif", d.info.get("exif"))) if v}

    if d.format == "JPEG":
        first.convert("RGB").save(buf, "JPEG", quality=cfg.quality, **extra)
    elif d.format == "PNG":
        first.save(buf, "PNG", **extra)
    elif d.format == "WEBP":
        opts = {"lossless": d.info.get("lossless", False), "quality": cfg.quality, **extra}
        if len(images) > 1:
            opts.update(save_all=True, append_images=images[1:], duration=d.durations, loop=d.loop)
        first.save(buf, "WEBP", **opts)
    elif d.format == "AVIF":
        opts = {"quality": cfg.quality, "speed": cfg.avif_speed, **extra}
        if len(images) > 1:
            opts.update(save_all=True, append_images=images[1:], duration=d.durations, loop=d.loop)
        first.save(buf, "AVIF", **opts)
    elif d.format == "GIF":
        opts = {}
        if len(images) > 1:
            opts.update(save_all=True, append_images=images[1:], duration=d.durations,
                        loop=d.loop, disposal=2)
        first.save(buf, "GIF", **opts)
    else:
        raise ValueError(f"cannot encode {d.format}")
    return buf.getvalue()


def apply_censor(frame: np.ndarray, box: Box, cfg: CensorConfig) -> None:
    """Censor `box` of an RGB(A) frame in place. Alpha is left alone."""
    if box.w <= 0 or box.h <= 0:
        return
    roi = frame[box.y:box.y + box.h, box.x:box.x + box.w, :3]
    if cfg.style == "solid":
        roi[:] = cfg.fill_color
    elif cfg.style == "pixelate":
        longest = max(box.w, box.h)
        bw = max(1, round(box.w * cfg.pixel_blocks / longest))
        bh = max(1, round(box.h * cfg.pixel_blocks / longest))
        small = cv2.resize(np.ascontiguousarray(roi), (bw, bh), interpolation=cv2.INTER_AREA)
        roi[:] = cv2.resize(small, (box.w, box.h), interpolation=cv2.INTER_NEAREST)
    else:
        roi[:] = _gaussian(roi, cfg.blur_strength)


def _gaussian(roi: np.ndarray, strength: float) -> np.ndarray:
    """Gaussian blur with a kernel proportional to the region size. Large
    kernels run on a downscaled copy, which looks the same and is far faster."""
    h, w = roi.shape[:2]
    k = max(3, int(max(w, h) * strength))
    scale = max(1, k // 15)
    small = cv2.resize(np.ascontiguousarray(roi), (max(1, w // scale), max(1, h // scale)),
                       interpolation=cv2.INTER_AREA)
    ks = max(3, (k // scale) | 1)
    small = cv2.GaussianBlur(small, (ks, ks), 0)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)
