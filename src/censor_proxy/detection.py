"""NudeNet wrapper and the female-only selection rule."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field, replace

import cv2
import nudenet
import numpy as np
import onnxruntime
from nudenet import NudeDetector
from nudenet.nudenet import _read_image

from .config import DetectionConfig, ModelConfig

BUNDLED_320N = os.path.join(os.path.dirname(nudenet.__file__), "320n.onnx")
# The model's output classes, in order (NudeNet's module-private __labels).
LABELS: tuple[str, ...] = tuple(vars(nudenet.nudenet)["__labels"])
# NudeNet's postprocessing constants: minimum class score, NMS score and IoU.
MIN_SCORE, NMS_SCORE, NMS_IOU = 0.2, 0.25, 0.45


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

    def intersection(self, other: Box) -> int:
        ix = max(0, min(self.x + self.w, other.x + other.w) - max(self.x, other.x))
        iy = max(0, min(self.y + self.h, other.y + other.h) - max(self.y, other.y))
        return ix * iy

    def iou(self, other: Box) -> float:
        inter = self.intersection(other)
        union = self.w * self.h + other.w * other.h - inter
        return inter / union if union else 0.0

    def overlap(self, other: Box) -> float:
        """Intersection as a fraction of the smaller box: 1 when one box is
        inside the other."""
        smaller = min(self.w * self.h, other.w * other.h)
        return self.intersection(other) / smaller if smaller else 0.0

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
    # Every class's score for this box (the model scores all classes per box
    # and `label` is the best). Empty for detections not from the model.
    scores: dict[str, float] = field(default_factory=dict, compare=False, repr=False)

    def score_for(self, label: str) -> float:
        return self.score if label == self.label else self.scores.get(label, 0.0)

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
        self.tiling = cfg.tiling
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
        """`bgr` is an HxWx3 uint8 array in OpenCV channel order.

        With tiling on, tall or wide images are also scanned as overlapping
        square tiles, so small regions aren't lost when the whole frame is
        shrunk to the model's input size. Duplicates across tiles are merged.
        """
        found = self._detect_one(bgr)
        if not self.tiling:
            return found
        for x, y, size in tiles(*bgr.shape[:2]):
            crop = np.ascontiguousarray(bgr[y:y + size, x:x + size])
            found += [replace(d, box=Box(d.box.x + x, d.box.y + y, d.box.w, d.box.h))
                      for d in self._detect_one(crop)]
        return merge(found)

    def _detect_one(self, bgr: np.ndarray) -> list[Detection]:
        """NudeNet's detect(), keeping every class's score per box: its own
        postprocessing keeps only the best one."""
        size = self._nd.input_width
        blob, _, _, x_pad, y_pad, width, height = _read_image(bgr, size)
        output = self._nd.onnx_session.run(None, {self._nd.input_name: blob})
        return postprocess(output[0], x_pad, y_pad, width, height, size)


def postprocess(output: np.ndarray, x_pad: int, y_pad: int, width: int, height: int,
                size: int) -> list[Detection]:
    """NudeNet's _postprocess, vectorised and keeping all class scores.

    `output` is the model's (1, 4 + classes, anchors) array: centre box in
    model-input pixels, then a score per class. Boxes are the same as
    NudeNet's: best class per anchor, then class-agnostic NMS.
    """
    rows = np.squeeze(output, 0).T
    scores = rows[:, 4:]
    best = scores.max(axis=1)
    keep = best >= MIN_SCORE
    rows, scores, best = rows[keep], scores[keep], best[keep]
    cx, cy, w, h = rows[:, 0], rows[:, 1], rows[:, 2], rows[:, 3]
    sx, sy = np.float32((width + x_pad) / size), np.float32((height + y_pad) / size)
    x = np.clip((cx - w / 2) * sx, 0, width)
    y = np.clip((cy - h / 2) * sy, 0, height)
    w = np.minimum(w * sx, width - x)
    h = np.minimum(h * sy, height - y)
    boxes = np.stack([x, y, w, h], axis=1)
    indices = cv2.dnn.NMSBoxes(boxes.tolist(), best.tolist(), NMS_SCORE, NMS_IOU)
    return [
        Detection(LABELS[int(scores[i].argmax())], float(best[i]),
                  Box(*(int(v) for v in boxes[i])),
                  {label: float(v) for label, v in zip(LABELS, scores[i])})
        for i in np.asarray(indices, dtype=int).reshape(-1)
    ]


def tiles(height: int, width: int) -> list[tuple[int, int, int]]:
    """Overlapping square tiles (x, y, size) along the long side of an image
    more than 1.2x longer than it is wide (or vice versa); none otherwise."""
    short, long_ = min(height, width), max(height, width)
    if long_ <= short * 1.2:
        return []
    n = math.ceil(long_ / short) + 1
    step = (long_ - short) / (n - 1)
    offsets = [round(i * step) for i in range(n)]
    if height > width:
        return [(0, o, short) for o in offsets]
    return [(o, 0, short) for o in offsets]


def merge(detections: list[Detection], iou: float = 0.4) -> list[Detection]:
    """Drop lower-scoring duplicates: same label, overlapping by more than `iou`."""
    kept: list[Detection] = []
    for d in sorted(detections, key=lambda d: -d.score):
        if not any(k.label == d.label and k.box.iou(d.box) > iou for k in kept):
            kept.append(d)
    return kept


def counterpart_scores(d: Detection, cfg: DetectionConfig) -> dict[str, float]:
    """The opposite-sex counterpart label's score for this box (see
    `male_counterparts`), for logs and review; empty if it has none."""
    pairs = {**cfg.male_counterparts, **{m: f for f, m in cfg.male_counterparts.items()}}
    other = pairs.get(d.label)
    return {other: round(d.score_for(other), 3)} if other and d.scores else {}


def decide(detections: list[Detection], cfg: DetectionConfig) -> list[Decision]:
    """Decide which detections to censor.

    Female labels are censored above their threshold, which context moves:
    lower when the box touches a confident female detection (e.g. the second
    breast of a pair), higher when nothing else in the image is female but
    male context is present. With male_score_ratio set, a female box also
    passes when the model scores the box's male counterpart label close to
    the female one. A box of a label in yields_to_male passes when it
    overlaps a confident male_priority_labels box (a penis), so the blur
    never lands on it. Unsexed labels are censored unless the box is
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
    male_priority = [d for d in detections
                     if d.label in cfg.male_priority_labels and d.score >= cfg.male_priority_threshold]

    def covered_male(d: Detection) -> Detection | None:
        if d.label not in cfg.yields_to_male:
            return None
        return max((m for m in male_priority if d.box.overlap(m.box) >= cfg.male_priority_overlap),
                   key=lambda m: m.score, default=None)

    # A box that gives way to a penis doesn't corroborate its neighbours.
    confident_female = [d for d in detections
                        if d.label in cfg.female_labels and d.score >= cfg.threshold_for(d.label)
                        and not covered_male(d)]

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
            male = cfg.male_counterparts.get(d.label)
            if (censor and male and cfg.male_score_ratio
                    and (m := d.score_for(male)) >= cfg.male_score_ratio * d.score):
                censor, reason = False, f"{male} scores {m:.2f}"
            if censor and (p := covered_male(d)):
                censor, reason = False, f"covers {p.label} {p.score:.2f}"
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
