import base64
import io
import json

import numpy as np
import pytest
from conftest import FakeDetector, det
from PIL import Image

from censor_proxy.datauri import censor_data_uris, find
from censor_proxy.imaging import Censor

BOX = (40, 40, 60, 60)


def png(seed=0):
    rng = np.random.default_rng(seed)
    buf = io.BytesIO()
    Image.fromarray(rng.integers(0, 256, (160, 200, 3), dtype=np.uint8)).save(buf, "PNG")
    return buf.getvalue()


def uri(data, mime="png"):
    return b"data:image/" + mime.encode() + b";base64," + base64.b64encode(data)


def payloads(body):
    return [base64.b64decode(m.group(2).replace(b"\\/", b"/")) for m in find(body)]


@pytest.fixture
def censor(cfg):
    return Censor(cfg, FakeDetector([det("FEMALE_BREAST_EXPOSED", box=BOX)]))


def test_html_img_srcset_and_style(censor):
    a, b, c = png(1), png(2), png(3)
    body = (b'<img src="' + uri(a) + b'" srcset="' + uri(b) + b' 2x">'
            b'<div style="background:url(' + uri(c) + b')"></div>')
    result = censor_data_uris(body, censor, fail_closed=True)
    assert result.changed and len(result.results) == 3
    out = payloads(result.body)
    assert len(out) == 3 and all(o != i for o, i in zip(out, (a, b, c)))
    assert result.body.startswith(b'<img src="data:image/png;base64,')
    assert result.body.endswith(b')"></div>')


def test_css_url(censor):
    body = b".hero { background-image: url('" + uri(png()) + b"'); color: red }"
    result = censor_data_uris(body, censor, fail_closed=True)
    assert result.changed and result.body.endswith(b"'); color: red }")


def test_json_with_escaped_slashes_round_trips(censor):
    data = png(4)
    escaped = uri(data).replace(b"/", b"\\/")
    assert b"\\/" in escaped  # PNG base64 nearly always contains "/"
    body = b'{"thumb": "' + escaped + b'", "n": 1}'
    result = censor_data_uris(body, censor, fail_closed=True)
    assert result.changed
    parsed = json.loads(result.body)  # still valid JSON
    assert parsed["n"] == 1
    new = base64.b64decode(parsed["thumb"].split(",", 1)[1])
    assert new != data and Image.open(io.BytesIO(new)).format == "PNG"


def test_clean_body_untouched(cfg):
    body = b'<img src="' + uri(png()) + b'">'
    result = censor_data_uris(body, Censor(cfg, FakeDetector()), fail_closed=True)
    assert not result.changed and result.body is body


def test_small_and_non_image_uris_skipped(make_config):
    cfg = make_config("cache: {max_entries: 0}\n")  # default 1 KB size floor
    fake = FakeDetector([det("FEMALE_BREAST_EXPOSED", box=BOX)])
    tiny = io.BytesIO()
    Image.new("RGB", (8, 8)).save(tiny, "PNG")
    body = (b'<img src="' + uri(tiny.getvalue()) + b'">'
            b'<img src="data:image/svg+xml;base64,PHN2Zy8+">'
            b'<img src="data:image/png;base64,!!notbase64!!">')
    result = censor_data_uris(body, Censor(cfg, fake), fail_closed=True)
    assert not result.changed and fake.calls == 0


def test_detector_error_fails_closed_per_image(cfg):
    def boom(bgr):
        raise RuntimeError("model exploded")
    result = censor_data_uris(b'"' + uri(png()) + b'"', Censor(cfg, FakeDetector(boom)), fail_closed=True)
    [(mime, r)] = result.results
    assert mime == "image/png" and r.action == "fail_closed" and result.changed


def test_detector_error_without_fail_closed_leaves_image(cfg):
    def boom(bgr):
        raise RuntimeError("model exploded")
    body = b'"' + uri(png()) + b'"'
    result = censor_data_uris(body, Censor(cfg, FakeDetector(boom)), fail_closed=False)
    assert not result.changed and result.results[0][1].action == "error"


def test_force_reason_fails_everything_closed_without_detection(cfg):
    fake = FakeDetector([det("FEMALE_BREAST_EXPOSED", box=BOX)])
    body = b'"' + uri(png(1)) + b'" "' + uri(png(2)) + b'"'
    result = censor_data_uris(body, Censor(cfg, fake), fail_closed=True, force_reason="timeout")
    assert fake.calls == 0 and [r.action for _, r in result.results] == ["fail_closed"] * 2


def test_jpeg_mime_variants_found():
    body = b"data:image/jpg;base64,AAAA data:image/jpeg;base64,BBBB data:image/webp;base64,CCCC"
    assert [m.group(1) for m in find(body)] == [b"jpg", b"jpeg", b"webp"]


def test_escaped_mime_slash_found():
    assert [m.group(1) for m in find(b'"data:image\\/png;base64,AAAA"')] == [b"png"]
