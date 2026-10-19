"""Replay video attribution offline against a score dump, for tuning.

    python tools/replay_attribution.py SCORES.json [SCORES.json ...] [--config FILE]

A dump comes from tools/dump_video_scores.py: every sampled frame's
detections from the video model, and the attribution model's detections
over the whole video. Each dump is replayed through the real Timeline and
decide(), with video_attribution forced on and its settings from the
config, and the tool prints how many samples are censored with and without
attribution, by label, plus the stretches that weren't attributed male.
Nothing is decoded or fetched, so settings can be compared in seconds.

Also reads the older list format (entries with i, d and, on every 4th, d640);
pass --seconds-per-sample for those (8 frames at 30 fps: 0.2667).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from censor_proxy.attribution import Timeline, evidence, male_detection  # noqa: E402
from censor_proxy.config import load_config  # noqa: E402
from censor_proxy.detection import Box, Detection, decide  # noqa: E402


def detections(entries: list[dict]) -> list[Detection]:
    return [Detection(e["label"], e["score"], Box(*e["box"]), e.get("s") or {}) for e in entries]


def load(path: Path, seconds_per_sample: float) -> tuple[list[tuple[float, list]], list[tuple[float, list]]]:
    """(samples, attribution): (time, detections) pairs for the video model's
    samples and the attribution model's frames."""
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        return ([(s["t"], detections(s["d"])) for s in data["samples"]],
                [(a["t"], detections(a["d"])) for a in data["attribution"]])
    return ([(e["i"] * seconds_per_sample, detections(e["d"])) for e in data],
            [(e["i"] * seconds_per_sample, detections(e["d640"])) for e in data if "d640" in e])


def thin(frames: list[tuple[float, list]], fps: float) -> list[tuple[float, list]]:
    """Keep frames at most `fps` per second, as the scanner would see them."""
    out, next_t = [], float("-inf")
    for t, d in frames:
        if t >= next_t - 1e-6:
            out.append((t, d))
            next_t = t + 1 / fps
    return out


def stretches(times: list[float], flags: list[bool], gap: float) -> list[tuple[float, float]]:
    """Time ranges where `flags` is false, merging samples closer than `gap`."""
    out: list[list[float]] = []
    for t, flag in zip(times, flags):
        if flag:
            continue
        if out and t - out[-1][1] <= gap:
            out[-1][1] = t
        else:
            out.append([t, t])
    return [(a, b) for a, b in out]


def replay(path: Path, cfg, seconds_per_sample: float) -> None:
    a = cfg.video_attribution
    samples, attribution = load(path, seconds_per_sample)
    frames = thin(attribution, a.sample_fps)
    timeline = Timeline(a)
    for t, found in frames:
        timeline.add(evidence(t, found, a))
    timeline.finish()
    male_cfg = male_detection(cfg.detection, a)

    before, after = Counter(), Counter()
    flags = []
    for t, found in samples:
        male = timeline.male_at(t)
        flags.append(male)
        for label in {d.detection.label for d in decide(found, cfg.detection) if d.censor}:
            before[label] += 1
        for label in {d.detection.label for d in decide(found, male_cfg if male else cfg.detection) if d.censor}:
            after[label] += 1
    censored = lambda cfg_for: sum(any(d.censor for d in decide(found, cfg_for(t)))
                                   for t, found in samples)
    n_before = censored(lambda t: cfg.detection)
    n_after = censored(lambda t: male_cfg if timeline.male_at(t) else cfg.detection)
    males = sum(f.male for f in timeline._evidence)
    females = sum(f.female for f in timeline._evidence)
    duration = samples[-1][0] if samples else 0

    print(f"{path.name}: {len(samples)} samples over {duration:.0f}s; "
          f"{len(frames)} attribution frames at <= {a.sample_fps:g}/s "
          f"({males} male, {females} female evidence)")
    print(f"  male stretches: {sum(flags)}/{len(flags)} samples ({sum(flags) / max(1, len(flags)):.0%})")
    print(f"  samples censored: {n_before} -> {n_after}")
    for label in sorted(set(before) | set(after)):
        print(f"    {label:28} {before[label]:5} -> {after[label]:5}")
    unattributed = stretches([t for t, _ in samples], flags, gap=2)
    shown = ", ".join(f"{s:.0f}-{e:.0f}s" for s, e in unattributed[:20])
    more = f" (+{len(unattributed) - 20} more)" if len(unattributed) > 20 else ""
    print(f"  not male: {shown or 'none'}{more}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dumps", nargs="+", type=Path)
    ap.add_argument("--config", help="config file (default: none, the packaged defaults)")
    ap.add_argument("--seconds-per-sample", type=float, default=0.25,
                    help="sample spacing for old list-format dumps")
    args = ap.parse_args()
    cfg = load_config(args.config)
    cfg = replace(cfg, video_attribution=replace(cfg.video_attribution, enabled=True))
    for path in args.dumps:
        replay(path, cfg, args.seconds_per_sample)


if __name__ == "__main__":
    main()
