"""Record a video job's detection scores, for tuning video attribution.

    python tools/dump_video_scores.py SOURCE OUT.json [--config FILE]
        [--referer URL] [--user-agent UA] [--attribution-fps N]

Runs the real job path (censor_video) on SOURCE (a file, or an HLS/DASH/MP4
URL from the proxy log) with video_attribution on, and writes every sampled
frame's detections from the video model (with all class scores) and the
attribution model's detections, with their times. No frames are kept: the
censored output goes to a temporary file that is deleted. Replay the dump
with tools/replay_attribution.py.

The attribution pass runs at --attribution-fps (default 2), so the replay
can try any rate up to that. Pass the source page as --referer: some CDNs
answer 410 without it. Fetching a video needs the user's go-ahead.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from censor_proxy import video  # noqa: E402
from censor_proxy.config import load_config  # noqa: E402
from censor_proxy.detection import Detector  # noqa: E402

BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/141.0.0.0 Safari/537.36")


def record(d, scores: bool) -> dict:
    out = {"label": d.label, "score": round(d.score, 3), "box": [d.box.x, d.box.y, d.box.w, d.box.h]}
    if scores:
        out["s"] = {k: round(x, 3) for k, x in d.scores.items() if x >= 0.05}
    return out


class Recording:
    """Wraps a detector, keeping each call's detections in order."""

    def __init__(self, detector: Detector, scores: bool):
        self.detector, self.scores, self.calls = detector, scores, []

    def detect(self, bgr):
        found = self.detector.detect(bgr)
        self.calls.append([record(d, self.scores) for d in found])
        return found


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source")
    ap.add_argument("out", type=Path)
    ap.add_argument("--config", help="config file (default: none, the packaged defaults)")
    ap.add_argument("--referer")
    ap.add_argument("--user-agent", default=BROWSER_UA)
    ap.add_argument("--attribution-fps", type=float, default=2.0)
    args = ap.parse_args()

    cfg = load_config(args.config)
    a = replace(cfg.video_attribution, enabled=True, sample_fps=args.attribution_fps)
    cfg = replace(cfg, video_attribution=a,
                  video=replace(cfg.video, abandon_seconds=1e9, idle_seconds=1e9,
                                max_duration_seconds=1e9))
    v = cfg.video
    samples = Recording(Detector(replace(cfg.model, path=v.model_path, resolution=v.model_resolution,
                                         tiling=v.tiling)), scores=True)
    attribution = Recording(Detector(replace(cfg.model, path=a.model_path, resolution=a.model_resolution,
                                             tiling=a.tiling)), scores=False)
    with tempfile.TemporaryDirectory() as tmp:
        job = video.Job("dump", args.source, referer=args.referer, user_agent=args.user_agent,
                        path=Path(tmp) / "out.mp4")
        p = video.probe(job)
        fps = min(p.fps, v.max_fps)
        step = max(1, round(fps / v.sample_fps))
        video.censor_video(job, cfg, samples, attribution)
    args.out.write_text(json.dumps({
        "source": args.source, "fps": fps, "step": step, "attribution_fps": a.sample_fps,
        "samples": [{"t": round(i * step / fps, 3), "d": d} for i, d in enumerate(samples.calls)],
        "attribution": [{"t": round(k / a.sample_fps, 3), "d": d}
                        for k, d in enumerate(attribution.calls)],
    }))
    print(f"{len(samples.calls)} samples, {len(attribution.calls)} attribution frames, "
          f"{job.seconds_done:.0f}s -> {args.out}")


if __name__ == "__main__":
    main()
