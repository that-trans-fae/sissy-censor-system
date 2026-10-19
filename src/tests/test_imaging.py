import io

import numpy as np
import pytest
from conftest import FakeDetector, det
from PIL import Image

from censor_proxy.imaging import Censor


def noise(w=200, h=160, mode="RGB", seed=0):
    rng = np.random.default_rng(seed)
    return Image.fromarray(rng.integers(0, 256, (h, w, len(mode)), dtype=np.uint8), mode)


def encode(im, fmt, **kw):
    buf = io.BytesIO()
    im.save(buf, fmt, **kw)
    return buf.getvalue()


def decode(data):
    return np.array(Image.open(io.BytesIO(data)).convert("RGB")).astype(int)


BOX = (40, 40, 60, 60)


@pytest.mark.parametrize("fmt,kw", [("PNG", {}), ("WEBP", {"lossless": True}), ("GIF", {}),
                                    ("AVIF", {"quality": 100})])
def test_only_the_box_changes(cfg, fmt, kw):
    original = encode(noise(), fmt, **kw)
    result = Censor(cfg, FakeDetector([det("FEMALE_BREAST_EXPOSED", box=BOX)])).censor_bytes(original)
    assert result.action == "censored"
    before, after = decode(original), decode(result.data)
    assert Image.open(io.BytesIO(result.data)).format == fmt
    # Padded box is 34..106 on both axes; outside a margin past that, untouched.
    diff = np.abs(before - after).sum(axis=2) > 0
    assert diff[50:90, 50:90].mean() > 0.9
    if fmt == "AVIF":  # lossy everywhere: compare change, not exact pixels
        changed = np.abs(before - after).sum(axis=2)
        assert changed[50:90, 50:90].mean() > 5 * changed[:, 120:].mean()
    elif fmt != "GIF":  # GIF re-quantizes the whole palette
        assert not diff[:, 110:].any() and not diff[110:, :].any()


def test_jpeg_censored_and_keeps_format(cfg):
    original = encode(noise(), "JPEG", quality=95)
    result = Censor(cfg, FakeDetector([det("FEMALE_GENITALIA_EXPOSED", box=BOX)])).censor_bytes(original)
    assert result.action == "censored"
    assert Image.open(io.BytesIO(result.data)).format == "JPEG"


def test_clean_image_returned_byte_identical(cfg):
    original = encode(noise(), "JPEG")
    result = Censor(cfg, FakeDetector([det("MALE_GENITALIA_EXPOSED", box=BOX)])).censor_bytes(original)
    assert result.action == "clean" and result.data is original


def test_alpha_preserved(cfg):
    im = noise(mode="RGBA")
    original = encode(im, "PNG")
    result = Censor(cfg, FakeDetector([det("FEMALE_BREAST_EXPOSED", box=BOX)])).censor_bytes(original)
    out = Image.open(io.BytesIO(result.data))
    assert out.mode == "RGBA"
    assert np.array_equal(np.array(out)[..., 3], np.array(im)[..., 3])


@pytest.mark.parametrize("style", ["gaussian", "pixelate", "solid"])
def test_styles(make_config, style):
    cfg = make_config(f"censor: {{style: {style}, fill_color: [255, 0, 0]}}\n"
                      "images: {size_floor_bytes: 0, min_side_px: 1}\n")
    original = encode(noise(), "PNG")
    result = Censor(cfg, FakeDetector([det("FEMALE_BREAST_EXPOSED", box=BOX)])).censor_bytes(original)
    region = decode(result.data)[50:90, 50:90]
    if style == "solid":
        assert (region == [255, 0, 0]).all()
    else:
        # Censored region has far less local variation than the noise it replaced.
        assert np.abs(np.diff(region, axis=1)).mean() < 0.5 * np.abs(np.diff(decode(original)[50:90, 50:90], axis=1)).mean()


def test_animated_gif_roundtrip_and_frame_sampling(make_config):
    cfg = make_config("images: {size_floor_bytes: 0, min_side_px: 1, frame_step: 2}\n")
    frames = [noise(seed=i) for i in range(5)]
    original = encode(frames[0], "GIF", save_all=True, append_images=frames[1:],
                      duration=[50, 60, 70, 80, 90], loop=3)
    # Only frame 2 (a sampled frame) has a detection; its neighbours 1 and 3 inherit it.
    calls = []

    def detect(bgr):
        calls.append(len(calls))
        return [det("FEMALE_BREAST_EXPOSED", box=BOX)] if len(calls) == 2 else []

    result = Censor(cfg, FakeDetector(detect)).censor_bytes(original)
    assert len(calls) == 3  # frames 0, 2, 4
    out = Image.open(io.BytesIO(result.data))
    assert out.n_frames == 5 and out.info["loop"] == 3
    durations = []
    changed = []
    for i in range(5):
        out.seek(i)
        durations.append(out.info["duration"])
        src = Image.open(io.BytesIO(original))
        src.seek(i)
        a = np.array(src.convert("RGB")).astype(int)[50:90, 50:90]
        b = np.array(out.convert("RGB")).astype(int)[50:90, 50:90]
        changed.append(np.abs(np.diff(b, axis=1)).mean() < 0.5 * np.abs(np.diff(a, axis=1)).mean())
    assert durations == [50, 60, 70, 80, 90]
    assert changed == [False, True, True, True, False]


def test_fail_closed_blurs_everything(cfg):
    original = encode(noise(), "PNG")
    result = Censor(cfg, FakeDetector()).fail_closed(original, "timeout")
    assert result.action == "fail_closed"
    before, after = decode(original), decode(result.data)
    assert (np.abs(before - after).sum(axis=2) > 0).mean() > 0.95


def test_size_floor_and_min_side_skip(make_config):
    cfg = make_config("images: {size_floor_bytes: 100000}\n")
    fake = FakeDetector([det("FEMALE_BREAST_EXPOSED")])
    assert Censor(cfg, fake).censor_bytes(encode(noise(), "JPEG")).action == "skipped"
    cfg = make_config("images: {size_floor_bytes: 0, min_side_px: 500}\n")
    assert Censor(cfg, fake).censor_bytes(encode(noise(), "PNG")).action == "skipped"
    assert fake.calls == 0


def test_non_image_and_unsupported_skipped(cfg):
    fake = FakeDetector([det("FEMALE_BREAST_EXPOSED")])
    assert Censor(cfg, fake).censor_bytes(b"<svg/>" * 100, "image/svg+xml").action == "skipped"
    assert Censor(cfg, fake).censor_bytes(encode(noise(), "BMP")).action == "skipped"


def test_cache_hit(make_config):
    cfg = make_config("images: {size_floor_bytes: 0, min_side_px: 1}\n")
    fake = FakeDetector([det("FEMALE_BREAST_EXPOSED", box=BOX)])
    censor = Censor(cfg, fake)
    original = encode(noise(), "PNG")
    first, second = censor.censor_bytes(original), censor.censor_bytes(original)
    assert fake.calls == 1 and second.cached and second.data == first.data


@pytest.mark.parametrize("limit", ["max_bytes: 1000", "max_decoded_bytes: 1000", "max_frames: 3"])
def test_oversized_animation_fails_closed_to_first_frame(make_config, limit):
    cfg = make_config(f"images: {{size_floor_bytes: 0, min_side_px: 1, {limit}}}\n")
    frames = [noise(seed=i) for i in range(5)]
    original = encode(frames[0], "GIF", save_all=True, append_images=frames[1:], duration=50, loop=0)
    fake = FakeDetector([det("FEMALE_BREAST_EXPOSED", box=BOX)])
    result = Censor(cfg, fake).censor_bytes(original)
    assert result.action == "fail_closed" and fake.calls == 0
    out = Image.open(io.BytesIO(result.data))
    assert out.format == "GIF" and getattr(out, "n_frames", 1) == 1
    assert (np.abs(decode(original) - decode(result.data)).sum(axis=2) > 0).mean() > 0.9
