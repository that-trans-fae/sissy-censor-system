import numpy as np
from conftest import det

from censor_proxy.detection import LABELS, Box, Detection, Detector, merge, postprocess, tiles


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


class FakeModel:
    """Reports one box at the tile-local origin of every bottom-half crop."""

    def __init__(self):
        self.shapes = []

    def __call__(self, bgr):
        self.shapes.append(bgr.shape[:2])
        if bgr.shape[0] == bgr.shape[1] and bgr[0, 0, 0] == 255:
            return [Detection("FEMALE_BREAST_EXPOSED", 0.6, Box(10, 20, 30, 40),
                              {"FEMALE_BREAST_EXPOSED": 0.6, "MALE_BREAST_EXPOSED": 0.3})]
        return []


def make_detector(tiling):
    d = Detector.__new__(Detector)
    d.tiling, d._detect_one = tiling, FakeModel()
    return d


def test_tile_detections_mapped_to_image_coordinates():
    img = np.zeros((300, 100, 3), np.uint8)
    img[200:, :, 0] = 255  # only the last tile starts on a marked pixel
    d = make_detector(tiling=True)
    found = d.detect(img)
    assert d._detect_one.shapes[0] == (300, 100)
    assert len(d._detect_one.shapes) == 1 + len(tiles(300, 100))
    assert [(f.box.x, f.box.y, f.box.w, f.box.h) for f in found] == [(10, 220, 30, 40)]
    assert found[0].score_for("MALE_BREAST_EXPOSED") == 0.3  # class scores survive the shift


def test_tiling_off_runs_once():
    d = make_detector(tiling=False)
    d.detect(np.zeros((300, 100, 3), np.uint8))
    assert len(d._detect_one.shapes) == 1


def anchor(cx, cy, w, h, **scores):
    row = np.zeros(4 + len(LABELS), np.float32)
    row[:4] = cx, cy, w, h
    for label, v in scores.items():
        row[4 + LABELS.index(label)] = v
    return row


def test_postprocess_keeps_every_class_score():
    output = np.stack([
        anchor(50, 50, 40, 40, FEMALE_BREAST_EXPOSED=0.6, MALE_BREAST_EXPOSED=0.45),
        # Overlaps the first with a lower best score: removed by NMS.
        anchor(52, 50, 40, 40, MALE_BREAST_EXPOSED=0.5),
        anchor(200, 100, 20, 20, FACE_MALE=0.9, FACE_FEMALE=0.05),
        anchor(300, 100, 20, 20, FACE_FEMALE=0.15),  # below the minimum score
    ]).T[None]
    # A 640x320 image padded to square and scanned at 320: coordinates double.
    found = postprocess(output, x_pad=0, y_pad=320, width=640, height=320, size=320)
    breast, face = sorted(found, key=lambda d: d.box.x)
    assert (breast.label, breast.box) == ("FEMALE_BREAST_EXPOSED", Box(60, 60, 80, 80))
    assert round(breast.score, 3) == 0.6
    assert round(breast.score_for("MALE_BREAST_EXPOSED"), 3) == 0.45
    assert breast.score_for("FACE_FEMALE") == 0.0
    assert (face.label, face.box) == ("FACE_MALE", Box(380, 180, 40, 40))
    assert round(face.score_for("FACE_FEMALE"), 3) == 0.05
