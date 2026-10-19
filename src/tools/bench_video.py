"""Time each stage of a video job alone (decode, detection, blur, encode),
then a whole job as the proxy runs it, with where its frame loop waits.

Run in the image, from src/ (the clip must be under the mount):

    docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD/..:/w" -w /w/src \
        censor-proxy python tools/bench_video.py CLIP [--size 1920x1080] [--loop N]

--size scales and crops the clip to fill that size and blends it up to
30 fps, standing in for a typical source. Uses config/config.yaml (or
--config), with video.hwaccel as set there; prints timings only, so it is
safe to run on the explicit samples.
"""

import argparse
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from censor_proxy import hwaccel, video  # noqa: E402
from censor_proxy.config import load_config  # noqa: E402
from censor_proxy.detection import Detector, decide  # noqa: E402
from censor_proxy.imaging import apply_censor  # noqa: E402


def children_cpu() -> float:
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clip")
    ap.add_argument("--config", default="config/config.yaml")
    ap.add_argument("--size", help="WxH to scale the clip to, at 30 fps")
    ap.add_argument("--loop", type=int, default=0, help="extra times to repeat the clip")
    ap.add_argument("--threads", type=int, help="model.threads_per_worker (before video.max_threads)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    v = replace(cfg.video, abandon_seconds=1e9, idle_seconds=1e9, stall_seconds=0,
                source_protocols=(*cfg.video.source_protocols, "file"))
    m = cfg.model if args.threads is None else replace(cfg.model, threads_per_worker=args.threads)
    cfg = replace(cfg, video=v, model=m, video_attribution=replace(cfg.video_attribution, enabled=False))
    cv2.setNumThreads(cfg.censor.opencv_threads or -1)
    tmp = Path(tempfile.mkdtemp(prefix="bench-video-"))
    try:
        run(args, cfg, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run(args, cfg, tmp: Path) -> None:
    v = cfg.video
    src = str(tmp / "src.mp4")
    cmd = ["ffmpeg", "-v", "error", "-y", "-stream_loop", str(args.loop), "-i", args.clip]
    if args.size:
        w, h = args.size.split("x")
        cmd += ["-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
                "minterpolate=fps=30:mi_mode=blend", "-c:v", "libx264", "-preset", "fast", "-crf", "18"]
    else:
        cmd += ["-c", "copy"]
    subprocess.run([*cmd, src], check=True)
    p = video.probe(video.Job(id="bench", src=src), v.source_protocols)
    w, h = video.output_size(p, v.max_height)
    fps = min(p.fps, v.max_fps)
    step = max(1, round(fps / v.sample_fps))
    dur = p.duration
    method = hwaccel.method(v)
    threads = min(cfg.model.threads_per_worker, v.max_threads)
    print(f"source {p.width}x{p.height} {p.fps:.3g} fps {dur:.1f}s audio={p.audio_index is not None}"
          f" -> {w}x{h} {fps:.3g} fps, detection every {step} frames on {threads} threads,"
          f" {method} codecs, opencv threads {cv2.getNumThreads()}")

    def report(name, wall, cpu):
        print(f"  {name:<26} wall {wall:6.2f}s  cpu {cpu:6.2f}s  {dur / wall:6.2f}x realtime"
              f"  {cpu / dur:5.2f} cores at realtime")

    print("each stage alone:")
    raw = tmp / "frames.rgb"
    c0, t0 = children_cpu(), time.monotonic()
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", *hwaccel.decode_args(method, v), "-i", src,
                    "-map", f"0:{p.video_index}", "-vf", f"scale={w}:{h}", "-r", f"{fps:.6g}",
                    "-f", "rawvideo", "-pix_fmt", "rgb24", "-y", str(raw)], check=True)
    report("decode", time.monotonic() - t0, children_cpu() - c0)

    frames = np.fromfile(raw, np.uint8).reshape(-1, h, w, 3)
    raw.unlink()
    detector = Detector(replace(cfg.model, path=v.model_path, resolution=v.model_resolution,
                                tiling=v.tiling, threads_per_worker=threads))
    detector.detect(cv2.cvtColor(frames[0], cv2.COLOR_RGB2BGR))  # warm-up
    boxes_at = {}
    c0, t0 = time.process_time(), time.monotonic()
    for i in range(0, len(frames), step):
        bgr = cv2.cvtColor(frames[i], cv2.COLOR_RGB2BGR)
        boxes_at[i] = [d.detection.box.padded(v.padding, w, h)
                       for d in decide(detector.detect(bgr), cfg.detection) if d.censor]
    report(f"detection ({len(boxes_at)} samples)", time.monotonic() - t0, time.process_time() - c0)
    print(f"    {sum(map(bool, boxes_at.values()))} of {len(boxes_at)} samples have boxes")

    c0, t0 = time.process_time(), time.monotonic()
    for i in range(len(frames)):
        s = i - i % step
        for b in boxes_at.get(s, []) + boxes_at.get(s - step, []):
            apply_censor(frames[i], b, cfg.censor)
    report("blur", time.monotonic() - t0, time.process_time() - c0)

    out = tmp / "enc"
    out.mkdir()
    c0, t0 = children_cpu(), time.monotonic()
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", *hwaccel.encode_device_args(method, v),
                    "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}", "-r", f"{fps:.6g}",
                    "-i", "pipe:0", *hwaccel.encode_args(method, v), "-f", "hls",
                    "-hls_segment_type", "fmp4", "-hls_time", "4", "-hls_list_size", "0",
                    "-hls_segment_filename", str(out / "s-%05d.m4s"), str(out / "p.m3u8")],
                   input=frames.tobytes(), check=True)
    report("encode", time.monotonic() - t0, children_cpu() - c0)
    del frames

    print("whole job:")
    waits = {"read": 0.0, "write": 0.0}

    class Timed:
        def __init__(self, f, kind):
            self.f, self.kind = f, kind

        def read(self, n):
            t = time.monotonic()
            b = self.f.read(n)
            waits[self.kind] += time.monotonic() - t
            return b

        def write(self, b):
            t = time.monotonic()
            r = self.f.write(b)
            waits[self.kind] += time.monotonic() - t
            return r

    pump = video._pump
    video._pump = lambda s, d, *a, **k: pump(Timed(s, "read"), Timed(d, "write"), *a, **k)
    detect = detector.detect
    detecting = [0.0]

    def timed_detect(bgr):
        t = time.monotonic()
        r = detect(bgr)
        detecting[0] += time.monotonic() - t
        return r
    detector.detect = timed_detect
    job = video.Job(id="bench", src=src, out_dir=tmp / "job")
    c0, p0, t0 = children_cpu(), time.process_time(), time.monotonic()
    video.censor_video(job, cfg, detector)
    wall = time.monotonic() - t0
    ff, py = children_cpu() - c0, time.process_time() - p0
    report("job", wall, ff + py)
    print(f"    cpu: ffmpeg {ff:.2f}s, python and onnxruntime {py:.2f}s")
    print(f"    frame loop: detection {detecting[0]:.2f}s, waiting on the decoder {waits['read']:.2f}s,"
          f" rest {wall - detecting[0] - waits['read']:.2f}s;"
          f" writer thread waiting on the encoder {waits['write']:.2f}s")


if __name__ == "__main__":
    main()
