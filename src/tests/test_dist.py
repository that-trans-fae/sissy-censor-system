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


def test_dist_files():
    assert os.access(SRC / "dist" / "scs-update", os.X_OK)
    dockerfile = (SRC / "Dockerfile").read_text()
    assert "COPY dist/ dist/" in dockerfile and "HEALTHCHECK" in dockerfile
    dev = yaml.safe_load((SRC / "compose.yaml").read_text())["services"]["censor-proxy"]
    assert dev["build"]["additional_contexts"]["brand"] == "../docs/brand"
