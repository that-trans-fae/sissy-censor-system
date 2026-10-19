import shutil
from dataclasses import replace

import pytest

from censor_proxy import hwaccel

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


def video(cfg, **changes):
    return replace(cfg.video, **changes)


def test_none_and_missing_devices_use_software(cfg, monkeypatch, tmp_path):
    monkeypatch.setattr(hwaccel, "probe", lambda *a: pytest.fail("nothing to test"))
    monkeypatch.setattr(hwaccel, "NVIDIA_DEVICES", ())
    assert hwaccel.method(video(cfg, hwaccel="none")) == hwaccel.SOFTWARE
    v = video(cfg, hwaccel="auto", hwaccel_device=str(tmp_path / "renderD128"))
    assert hwaccel.current(v) is None
    assert hwaccel.method(v) == hwaccel.current(v) == hwaccel.SOFTWARE
    assert hwaccel.method(replace(v, hwaccel="vaapi")) == hwaccel.SOFTWARE


def test_auto_takes_the_first_method_that_works_once(cfg, monkeypatch):
    tried = []

    def probe(name, vcfg):
        tried.append(name)
        return "no VA display" if name == "vaapi" else ""
    monkeypatch.setattr(hwaccel, "_present", lambda name, vcfg: True)
    monkeypatch.setattr(hwaccel, "probe", probe)
    v = video(cfg, hwaccel="auto")
    assert hwaccel.method(v) == "nvenc" and hwaccel.method(v) == "nvenc"
    assert tried == ["vaapi", "nvenc"]
    # Failing on a real video that software then handled turns it off.
    hwaccel.disable("nvenc", "encode failed")
    assert hwaccel.method(v) == hwaccel.current(v) == hwaccel.SOFTWARE
    # A setting that names vaapi is a new choice, tested anew.
    assert hwaccel.method(replace(v, hwaccel="vaapi")) == hwaccel.SOFTWARE
    assert tried == ["vaapi", "nvenc", "vaapi"]


def test_encode_args(cfg):
    v = video(cfg, preset="faster", crf=21)
    assert hwaccel.encode_args("software", v) == ["-c:v", "libx264", "-preset", "faster",
                                                  "-crf", "21", "-pix_fmt", "yuv420p"]
    vaapi = hwaccel.encode_args("vaapi", v)
    assert vaapi[:2] == ["-vf", "format=nv12,hwupload"] and "h264_vaapi" in vaapi
    assert vaapi[vaapi.index("-qp") + 1] == "21" and vaapi[vaapi.index("-bf") + 1] == "0"
    assert hwaccel.encode_device_args("vaapi", v) == ["-vaapi_device", v.hwaccel_device]
    nvenc = hwaccel.encode_args("nvenc", v)
    assert "h264_nvenc" in nvenc and nvenc[nvenc.index("-pix_fmt") + 1] == "yuv420p"
    assert nvenc[nvenc.index("-forced-idr") + 1] == "1" and nvenc[nvenc.index("-cq") + 1] == "21"
    assert hwaccel.decode_args("software", v) == hwaccel.encode_device_args("nvenc", v) == []


@needs_ffmpeg
def test_probe_runs_the_encode_and_decode(cfg, tmp_path):
    assert hwaccel.probe("software", cfg.video) == ""
    error = hwaccel.probe("vaapi", video(cfg, hwaccel_device=str(tmp_path / "renderD128")))
    assert error and "vaapi" in error.lower()
