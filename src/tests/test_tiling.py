import numpy as np
from conftest import det

from censor_proxy.detection import Detector, merge, tiles


def test_no_tiles_for_near_square():
    assert tiles(1000, 1000) == [] and tiles(1100, 1000) == []


def test_tall_image_tiles_cover_height_with_overlap():
    t = tiles(1920, 1080)
    assert all(x == 0 and size == 1080 for x, _, size in t)
    ys = [y for _, y, _ in t]
    assert ys[0] == 0 and ys[-1] + 1080 == 1920
    assert all(b - a < 1080 for a, b in zip(ys, ys[1:]))  # consecutive tiles overlap


def test_wide_image_tiles_along_width():
    t = tiles(500, 1600)
    assert all(y == 0 and size == 500 for _, y, size in t)
    assert t[-1][0] + 500 == 1600


def test_merge_keeps_best_of_same_label_duplicates():
    a = det("FEMALE_BREAST_EXPOSED", 0.8, (100, 100, 50, 50))
    b = det("FEMALE_BREAST_EXPOSED", 0.4, (105, 102, 50, 50))
    c = det("FACE_FEMALE", 0.9, (100, 100, 50, 50))
    d = det("FEMALE_BREAST_EXPOSED", 0.5, (400, 400, 50, 50))
    assert merge([b, a, c, d]) == [c, a, d]


class FakeNude:
    """Reports one box at the tile-local origin of every bottom-half crop."""

    def __init__(self):
        self.shapes = []

    def detect(self, bgr):
        self.shapes.append(bgr.shape[:2])
        if bgr.shape[0] == bgr.shape[1] and bgr[0, 0, 0] == 255:
            return [{"class": "FEMALE_BREAST_EXPOSED", "score": 0.6, "box": [10, 20, 30, 40]}]
        return []


def make_detector(tiling):
    d = Detector.__new__(Detector)
    d.tiling, d._nd = tiling, FakeNude()
    return d


def test_tile_detections_mapped_to_image_coordinates():
    img = np.zeros((300, 100, 3), np.uint8)
    img[200:, :, 0] = 255  # only the last tile starts on a marked pixel
    d = make_detector(tiling=True)
    found = d.detect(img)
    assert d._nd.shapes[0] == (300, 100) and len(d._nd.shapes) == 1 + len(tiles(300, 100))
    assert [(f.box.x, f.box.y, f.box.w, f.box.h) for f in found] == [(10, 220, 30, 40)]


def test_tiling_off_runs_once():
    d = make_detector(tiling=False)
    d.detect(np.zeros((300, 100, 3), np.uint8))
    assert len(d._nd.shapes) == 1
