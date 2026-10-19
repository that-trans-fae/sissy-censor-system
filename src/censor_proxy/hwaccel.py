"""Hardware video decode and encode for video jobs (video.hwaccel).

A method is software (libx264), vaapi (Intel and AMD GPUs through
/dev/dri) or nvenc (NVIDIA, with NVDEC for decoding). The container only
sees a GPU when compose passes one in (dist/compose.vaapi.yaml or
compose.nvidia.yaml, which the installer adds when the host has one), so
auto tries each method whose device is there with a one-second test
encode and decode, and falls back to software. The result is kept until the
setting changes or a method fails on a real video that software then
handled (disable()).

`python -m censor_proxy.hwaccel` runs the same test and exits 0 when a
hardware method works; the installers use it to decide whether to keep a
GPU override.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import threading

from .config import HWACCEL_CHOICES, VideoConfig

log = logging.getLogger("censor_proxy.hwaccel")

SOFTWARE = "software"
METHODS = tuple(c for c in HWACCEL_CHOICES if c not in ("auto", "none"))  # in auto's order
# Device nodes whose presence makes a method worth testing: a render node
# for VAAPI; the NVIDIA control device, or WSL2's GPU device on Docker
# Desktop for Windows.
NVIDIA_DEVICES = ("/dev/nvidiactl", "/dev/dxg")
PROBE_TIMEOUT = 30

_lock = threading.Lock()
_chosen: dict[tuple[str, str], str] = {}  # (hwaccel, device) -> method
_disabled: set[str] = set()


def decode_args(method: str, vcfg: VideoConfig) -> list[str]:
    """Decoder options, before -i. Frames come back in system memory, and a
    codec the hardware can't decode falls back to software inside ffmpeg."""
    if method == "vaapi":
        return ["-hwaccel", "vaapi", "-hwaccel_device", vcfg.hwaccel_device]
    if method == "nvenc":
        return ["-hwaccel", "cuda"]
    return []


def encode_device_args(method: str, vcfg: VideoConfig) -> list[str]:
    """Encoder global options, before its inputs."""
    return ["-vaapi_device", vcfg.hwaccel_device] if method == "vaapi" else []


def encode_args(method: str, vcfg: VideoConfig) -> list[str]:
    """The video codec options for 8-bit 4:2:0 H.264, which every browser's
    Media Source Extensions play. No B-frames from the hardware encoders, so
    a forced keyframe always starts a closed GOP; keyframe placement itself
    is the caller's (-g, -force_key_frames)."""
    if method == "vaapi":
        return ["-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi",
                "-rc_mode", "CQP", "-qp", str(vcfg.crf), "-bf", "0"]
    if method == "nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", str(vcfg.crf),
                "-b:v", "0", "-bf", "0", "-forced-idr", "1", "-no-scenecut", "1",
                "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", vcfg.preset, "-crf", str(vcfg.crf), "-pix_fmt", "yuv420p"]


def method(vcfg: VideoConfig) -> str:
    """The method video jobs use now. The first call for a setting may take
    a few seconds (the test encodes)."""
    key = (vcfg.hwaccel, vcfg.hwaccel_device)
    with _lock:
        if key not in _chosen:
            _chosen[key] = _choose(vcfg)
        chosen = _chosen[key]
        return SOFTWARE if chosen in _disabled else chosen


def current(vcfg: VideoConfig) -> str | None:
    """The method in use, without testing: None until method() has chosen."""
    with _lock:
        chosen = _chosen.get((vcfg.hwaccel, vcfg.hwaccel_device))
        return SOFTWARE if chosen in _disabled else chosen


def choose_soon(vcfg: VideoConfig) -> None:
    """Run the choice in the background, so the first job doesn't wait for
    it and the panel can show it."""
    threading.Thread(target=method, args=(vcfg,), name="hwaccel", daemon=True).start()


def disable(name: str, reason: str) -> None:
    """Stop using a method that failed on a video software then handled."""
    if name == SOFTWARE:
        return
    with _lock:
        if name not in _disabled:
            log.warning("video: %s failed (%s); using software encoding until restart", name, reason)
            _disabled.add(name)


def _choose(vcfg: VideoConfig) -> str:
    if vcfg.hwaccel == "none":
        return SOFTWARE
    candidates = METHODS if vcfg.hwaccel == "auto" else (vcfg.hwaccel,)
    for name in candidates:
        if not _present(name, vcfg):
            if vcfg.hwaccel != "auto":
                log.warning("video: hwaccel %s has no device in the container; using software", name)
            continue
        error = probe(name, vcfg)
        if not error:
            log.info("video: using %s for decoding and encoding", name)
            return name
        log.log(logging.WARNING if vcfg.hwaccel != "auto" else logging.INFO,
                "video: %s unavailable: %s", name, error)
    return SOFTWARE


def _present(name: str, vcfg: VideoConfig) -> bool:
    if name == "vaapi":
        return os.path.exists(vcfg.hwaccel_device)
    return any(os.path.exists(d) for d in NVIDIA_DEVICES)


def probe(name: str, vcfg: VideoConfig) -> str:
    """Encode a second of test video with `name` the way jobs do, and decode
    it back. "" if both work, else ffmpeg's complaint."""
    with tempfile.TemporaryDirectory(prefix="hwaccel-") as tmp:
        out = os.path.join(tmp, "probe.mp4")
        steps = [
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", *encode_device_args(name, vcfg),
             "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=30:duration=1,format=rgb24",
             *encode_args(name, vcfg), "-g", "15", "-force_key_frames", "expr:eq(mod(n,15),0)", out],
            ["ffmpeg", "-nostdin", "-loglevel", "error", *decode_args(name, vcfg), "-i", out,
             "-vf", "scale=160:120", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        ]
        for cmd in steps:
            try:
                r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                   timeout=PROBE_TIMEOUT)
            except (OSError, subprocess.TimeoutExpired) as e:
                return str(e)
            if r.returncode != 0:
                return r.stderr.decode(errors="replace").strip()[-300:] or f"exit {r.returncode}"
    return ""


def main() -> int:
    from .config import load_config
    vcfg = load_config().video
    if vcfg.hwaccel == "none":
        print("video.hwaccel is none")
        return 1
    for name in METHODS if vcfg.hwaccel == "auto" else (vcfg.hwaccel,):
        if not _present(name, vcfg):
            print(f"{name}: no device")
            continue
        error = probe(name, vcfg)
        print(f"{name}: {error or 'works'}")
        if not error:
            return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
