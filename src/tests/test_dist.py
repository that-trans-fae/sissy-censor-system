"""dist/compose.yaml, what installs run, against the development compose.yaml."""

import os
import re
from pathlib import Path

import yaml

SRC = Path(__file__).parents[1]
VAR = re.compile(r"\$\{(\w+)(:?[-?])?([^}]*)\}")


def service(path):
    return yaml.safe_load(path.read_text())["services"]["censor-proxy"]


def expand(value):
    """${VAR:-default} to its default, recursively."""
    if isinstance(value, str):
        return VAR.sub(lambda m: m.group(3) if m.group(2) == ":-" else m.group(0), value)
    if isinstance(value, list):
        return [expand(v) for v in value]
    if isinstance(value, dict):
        return {k: expand(v) for k, v in value.items()}
    return value


def test_dist_compose_matches_development():
    dev, dist = service(SRC / "compose.yaml"), service(SRC / "dist" / "compose.yaml")
    # Only how the image is chosen differs.
    for key in ("build", "image", "pull_policy"):
        dev.pop(key, None)
        dist.pop(key, None)
    assert expand(dist) == expand(dev)


def test_dist_compose_variables_have_defaults():
    # .env may predate a variable a newer compose.yaml uses.
    text = (SRC / "dist" / "compose.yaml").read_text()
    for name, op, _ in VAR.findall(text):
        assert op == ":-" or name == "SCS_IMAGE", name


def test_gpu_overrides():
    # Only what the installers add for a GPU; anything else belongs in compose.yaml.
    vaapi = service(SRC / "dist" / "compose.vaapi.yaml")
    nvidia = service(SRC / "dist" / "compose.nvidia.yaml")
    assert sorted(vaapi) == ["devices", "group_add"] and sorted(nvidia) == ["deploy"]
    # The node lands where video.hwaccel_device looks.
    assert expand(vaapi["devices"]) == ["/dev/dri/renderD128:/dev/dri/renderD128"]
    from censor_proxy.config import from_override
    assert from_override({}).video.hwaccel_device == "/dev/dri/renderD128"
    device, = nvidia["deploy"]["resources"]["reservations"]["devices"]
    assert device["driver"] == "nvidia" and "video" in device["capabilities"]
    for name in ("compose.vaapi.yaml", "compose.nvidia.yaml"):
        for var, op, _ in VAR.findall((SRC / "dist" / name).read_text()):
            assert op == ":-", var


def test_dist_files():
    assert os.access(SRC / "dist" / "scs-update", os.X_OK)
    dockerfile = (SRC / "Dockerfile").read_text()
    assert "COPY dist/ dist/" in dockerfile and "HEALTHCHECK" in dockerfile
    dev = yaml.safe_load((SRC / "compose.yaml").read_text())["services"]["censor-proxy"]
    assert dev["build"]["additional_contexts"]["brand"] == "../docs/brand"
