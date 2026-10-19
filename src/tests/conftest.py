import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from censor_proxy import hwaccel  # noqa: E402
from censor_proxy.config import load_config  # noqa: E402
from censor_proxy.detection import Box, Detection  # noqa: E402


class FakeDetector:
    """Returns `detections` (a list, or a callable taking the BGR frame) on every call."""

    def __init__(self, detections=()):
        self.detections = detections if callable(detections) else list(detections)
        self.calls = 0

    def detect(self, bgr):
        self.calls += 1
        if callable(self.detections):
            return self.detections(bgr)
        return list(self.detections)


@pytest.fixture(autouse=True)
def fresh_hwaccel():
    """hwaccel keeps its choice for the process; each test starts without one."""
    hwaccel._chosen.clear()
    hwaccel._disabled.clear()
    yield


def det(label, score=0.9, box=(10, 10, 50, 50)):
    return Detection(label, score, Box(*box))


@pytest.fixture
def make_config(tmp_path):
    def make(text: str = ""):
        path = tmp_path / "config.yaml"
        path.write_text(text)
        return load_config(path)
    return make


@pytest.fixture
def cfg(make_config):
    # Thresholds pinned so selection-rule tests don't move with the defaults.
    return make_config("images: {size_floor_bytes: 0, min_side_px: 1}\ncache: {max_entries: 0}\n"
                       "detection: {preset: custom, threshold: 0.5, label_thresholds: {}, corroborated_threshold: 0.35,"
                       " male_context_threshold: 0.65, context_threshold: 0.4}\n")
