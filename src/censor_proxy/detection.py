"""NudeNet wrapper and the female-only selection rule."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

import nudenet
import numpy as np
import onnxruntime
from nudenet import NudeDetector

from .config import DetectionConfig, ModelConfig

BUNDLED_320N = os.path.join(os.path.dirname(nudenet.__file__), "320n.onnx")


@dataclass(frozen=True)
class Box:
    x: int
    y: int
    w: int
    h: int

    def gap(self, other: Box) -> float:
        """Euclidean distance between the two rectangles; 0 if they overlap."""
        dx = max(0, other.x - (self.x + self.w), self.x - (other.x + other.w))
        dy = max(0, other.y - (self.y + self.h), self.y - (other.y + other.h))
        return math.hypot(dx, dy)

    def padded(self, fraction: float, width: int, height: int) -> Box:
        px, py = int(self.w * fraction), int(self.h * fraction)
        x0, y0 = max(0, self.x - px), max(0, self.y - py)
        x1 = min(width, self.x + self.w + px)
        y1 = min(height, self.y + self.h + py)
        return Box(x0, y0, x1 - x0, y1 - y0)


@dataclass(frozen=True)
class Detection:
    label: str
    score: float
    box: Box

    def as_dict(self) -> dict:
        b = self.box
        return {"label": self.label, "score": round(self.score, 3), "box": [b.x, b.y, b.w, b.h]}


@dataclass(frozen=True)
class Decision:
    detection: Detection
    censor: bool
    reason: str


class Detector:
    """Thread-safe: onnxruntime sessions may be run concurrently."""

    def __init__(self, cfg: ModelConfig):
        path = cfg.path or BUNDLED_320N
        resolution = cfg.resolution if cfg.path else 320
        if not os.path.isfile(path):
            raise FileNotFoundError(f"model weights not found: {path}")
        # Everything runs locally; keep onnxruntime from emitting telemetry.
        onnxruntime.disable_telemetry_events()
        self._nd = NudeDetector(model_path=path, inference_resolution=resolution)
        # NudeDetector ignores its `providers` argument and offers no session
        # options, so replace its session with one whose thread count we set.
        opts = onnxruntime.SessionOptions()
        opts.intra_op_num_threads = cfg.threads_per_worker
        opts.inter_op_num_threads = 1
        self._nd.onnx_session = onnxruntime.InferenceSession(
            path, sess_options=opts, providers=["CPUExecutionProvider"]
        )

    def detect(self, bgr: np.ndarray) -> list[Detection]:
        """`bgr` is an HxWx3 uint8 array in OpenCV channel order."""
        return [
            Detection(r["class"], float(r["score"]), Box(*r["box"]))
            for r in self._nd.detect(bgr)
        ]


def decide(detections: list[Detection], cfg: DetectionConfig) -> list[Decision]:
    """Decide which detections to censor.

    Female labels are censored above their threshold, which context moves:
    lower when the box touches a confident female detection (e.g. the second
    breast of a pair), higher when nothing else in the image is female but
    male context is present. Unsexed labels are censored unless the box is
    near male context, not near female context, and not overlapping even
    weak female context (and male_context_exempts is on). Everything else
    passes.
    """
    female_ctx = [d for d in detections
                  if d.label in cfg.female_context_labels and d.score >= cfg.context_threshold]
    weak_female_ctx = [d for d in detections
                       if d.label in cfg.female_context_labels
                       and d.score >= cfg.overlap_context_threshold]
    male_ctx = [d for d in detections
                if d.label in cfg.male_context_labels and d.score >= cfg.context_threshold]
    confident_female = [d for d in detections
                        if d.label in cfg.female_labels and d.score >= cfg.threshold_for(d.label)]

    def near(d: Detection, ctx: list[Detection]) -> Detection | None:
        reach = cfg.proximity * max(d.box.w, d.box.h)
        candidates = [c for c in ctx if c is not d and d.box.gap(c.box) <= reach]
        return min(candidates, key=lambda c: d.box.gap(c.box), default=None)

    def female_threshold(d: Detection) -> tuple[float, str]:
        base = cfg.threshold_for(d.label)
        if c := near(d, confident_female):
            return min(base, cfg.corroborated_threshold), f"corroborated by {c.label}"
        if male_ctx and not any(c is not d for c in female_ctx):
            return max(base, cfg.male_context_threshold), "male context only"
        return base, ""

    decisions = []
    for d in detections:
        if d.label in cfg.female_labels:
            threshold, context = female_threshold(d)
            censor = d.score >= threshold
            reason = "female label" if censor else f"below threshold {threshold:g}"
            decisions.append(Decision(d, censor, f"{reason}, {context}" if context else reason))
        elif d.label not in cfg.unsexed_labels:
            decisions.append(Decision(d, False, "label not censored"))
        elif d.score < cfg.threshold_for(d.label):
            decisions.append(Decision(d, False, "below threshold"))
        elif f := near(d, female_ctx):
            decisions.append(Decision(d, True, f"near {f.label}"))
        elif f := next((c for c in weak_female_ctx if d.box.gap(c.box) == 0), None):
            decisions.append(Decision(d, True, f"overlaps {f.label} {f.score:.2f}"))
        elif cfg.male_context_exempts and (m := near(d, male_ctx)):
            decisions.append(Decision(d, False, f"near {m.label} only"))
        else:
            decisions.append(Decision(d, True, "unattributed"))
    return decisions
