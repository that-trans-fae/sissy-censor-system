"""Real-model regression test against the hand-confirmed sample boxes.

Needs the model weights (run inside the Docker image) and
tests/fixtures/expected.json, produced by tools/draw_boxes.py and confirmed
by eye before being copied there.
"""

import json
from pathlib import Path

import pytest

from censor_proxy.config import load_config

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "docs" / "samples"
EXPECTED = Path(__file__).with_name("fixtures") / "expected.json"
IOU_MIN = 0.7

if not EXPECTED.exists():
    pytest.skip("no confirmed fixture yet (tests/fixtures/expected.json)", allow_module_level=True)
if not SAMPLES.is_dir():
    pytest.skip(f"sample images not present ({SAMPLES}); they are not in git", allow_module_level=True)


def iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    return inter / (aw * ah + bw * bh - inter)


@pytest.fixture(scope="module")
def censor():
    from censor_proxy.imaging import Censor
    try:
        return Censor(load_config())
    except FileNotFoundError as e:
        pytest.skip(str(e))


@pytest.mark.parametrize("case", json.loads(EXPECTED.read_text()), ids=lambda c: c["file"])
def test_sample(censor, case):
    result = censor.censor_bytes((SAMPLES / case["file"]).read_bytes())
    assert result.action == case["action"]
    got = [d.detection.box for d in result.censored_decisions]
    got = [[b.x, b.y, b.w, b.h] for b in got]
    want = [d["box"] for d in case["detections"] if d["censor"]]
    assert len(got) == len(want), f"censored {len(got)} boxes, expected {len(want)}"
    for box in want:
        assert max((iou(box, g) for g in got), default=0) >= IOU_MIN, f"no match for {box}"
