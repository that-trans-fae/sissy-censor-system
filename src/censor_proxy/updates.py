"""Automatic updates, the container's side of them.

The update itself runs on the host (scs-update, from src/dist/), since only
the host can pull an image and recreate this container. This module is how
the two talk through the files they share:

  python -m censor_proxy.updates policy   prints updates.* for scs-update
  python -m censor_proxy.updates health   the image's HEALTHCHECK, which scs-update waits on
  /logs/update.json                       scs-update's last result, shown in the panel

The container never contacts Docker Hub itself.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

from .config import ConfigError, load_config

# scs-update writes this into the mounted logs folder (<install>/logs).
STATUS_FILE = Path("/logs/update.json")
# The image's release, set at build time (tools/release.sh); "dev" otherwise.
VERSION_ENV = "SCS_VERSION"
MAX_STATUS_BYTES = 65536
# The proxy's listening port inside the container (the image's CMD).
PROXY_URL = "http://127.0.0.1:8080"


def version() -> str:
    return os.environ.get(VERSION_ENV) or "dev"


def read_status(path: Path = STATUS_FILE) -> dict | None:
    """scs-update's last result, or None if it hasn't run (or the file is
    unreadable: it is only ever shown, never acted on)."""
    try:
        with open(path, "rb") as f:
            data = json.loads(f.read(MAX_STATUS_BYTES))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def policy_lines() -> list[str]:
    """updates.* as key=value lines. scs-update parses these strictly and
    never evaluates them, since it runs on the host."""
    u = load_config().updates
    return [f"enabled={int(u.enabled)}",
            f"check_hours={u.check_hours:g}",
            f"wait_for_browser={int(u.wait_for_browser)}"]


def healthy(proxy: str = PROXY_URL, timeout: float = 8) -> bool:
    """True once the proxy routes its health host and the models are loaded."""
    host = load_config().proxy.health_host
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy}))
    with opener.open(f"http://{host}/health", timeout=timeout) as r:
        return bool(json.loads(r.read()).get("ready"))


def main(argv: list[str]) -> int:
    if argv not in (["policy"], ["health"]):
        print("usage: python -m censor_proxy.updates policy|health", file=sys.stderr)
        return 2
    try:
        if argv == ["health"]:
            return 0 if healthy() else 1
        print("\n".join(policy_lines()))
    except (ConfigError, OSError, ValueError) as e:
        print(f"{argv[0]}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
