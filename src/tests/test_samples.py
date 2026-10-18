"""Real-model regression test against hand-confirmed sample boxes.

Each tests/fixtures/expected-<model>.json is a tools/draw_boxes.py run that
was confirmed by eye, and is checked with that model regardless of the
configured default. Needs the model weights (run inside the Docker image)
and the sample images, which are not in git.
"""

import json
from pathlib import Path

import pytest

from censor_proxy.config import load_config

ROOT = Path(__file__).resolve().parents[2]
SAMPLES = ROOT / "docs" / "samples"
FIXTURES = Path(__file__).with_name("fixtures")
IOU_MIN = 0.7

MODELS = {
    "640m": "model: {path: /app/models/640m.onnx, resolution: 640}\n",
    "320n": "model: {path: null, resolution: 320}\n",
}

if not SAMPLES.is_dir():
    pytest.skip(f"sample images not present ({SAMPLES}); they are not in git", allow_module_level=True)

CASES = [
    pytest.param(model, case, id=f"{model}:{case['file']}")
    for model in MODELS
    if (FIXTURES / f"expected-{model}.json").exists()
    for case in json.loads((FIXTURES / f"expected-{model}.json").read_text())
]
if not CASES:
    pytest.skip("no confirmed fixtures yet (tests/fixtures/expected-<model>.json)", allow_module_level=True)


def iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    return inter / (aw * ah + bw * bh - inter)


@pytest.fixture(scope="module")
def censors(tmp_path_factory):
    from censor_proxy.imaging import Censor
    loaded = {}

    def get(model):
        if model not in loaded:
            path = tmp_path_factory.mktemp(model) / "config.yaml"
            path.write_text(MODELS[model])
            try:
                loaded[model] = Censor(load_config(path))
            except FileNotFoundError as e:
                pytest.skip(str(e))
        return loaded[model]
    return get


@pytest.mark.parametrize("model,case", CASES)
def test_sample(censors, model, case):
    result = censors(model).censor_bytes((SAMPLES / case["file"]).read_bytes())
    assert result.action == case["action"]
    got = [[d.detection.box.x, d.detection.box.y, d.detection.box.w, d.detection.box.h]
           for d in result.censored_decisions]
    want = [d["box"] for d in case["detections"] if d["censor"]]
    assert len(got) == len(want), f"censored {len(got)} boxes, expected {len(want)}"
    for box in want:
        assert max((iou(box, g) for g in got), default=0) >= IOU_MIN, f"no match for {box}"
