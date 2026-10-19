"""dist/scs-update, the host-side updater, against a fake docker on PATH."""

import io
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "dist" / "scs-update"
OLD = "hub/scs@sha256:" + "a" * 64
NEW = "hub/scs@sha256:" + "b" * 64

# Answers the docker commands scs-update runs, from state.json in
# $FAKE_DOCKER, and records each call in calls.jsonl.
FAKE_DOCKER = r'''#!/usr/bin/env python3
import io, json, os, sys, tarfile
from pathlib import Path

home = Path(os.environ["FAKE_DOCKER"])
state = json.loads((home / "state.json").read_text())
args = sys.argv[1:]
with open(home / "calls.jsonl", "a") as f:
    f.write(json.dumps(args) + "\n")

def env_image():
    for line in Path(".env").read_text().splitlines():
        if line.startswith("SCS_IMAGE="):
            return line.split("=", 1)[1]

if args[:1] == ["info"]:
    sys.exit(0)
if args[:2] == ["compose", "ps"]:
    print("cid1")
elif args[:2] == ["compose", "exec"] or args[:2] == ["compose", "run"]:
    print(state["policy"])
elif args[:2] == ["compose", "up"]:
    state["running"] = env_image()
    (home / "state.json").write_text(json.dumps(state))
elif args[:2] == ["compose", "logs"]:
    print("Traceback: the new release fell over")
elif args[:1] == ["pull"]:
    sys.exit(1 if state.get("offline") else 0)
elif args[:2] == ["image", "inspect"]:
    fmt, ref = args[3], args[4]
    if "RepoDigests" in fmt:
        print(state["latest"])
    else:
        print(state["versions"].get(ref, ""))
elif args[:1] == ["inspect"]:
    if "Health" in args[2]:
        health = "unhealthy" if state["running"] in state.get("broken", []) else "healthy"
        print(f"running {health} 0")
    else:
        print("running")
elif args[:1] == ["run"] and "tar" in args:
    ref = args[args.index("tar") + 1]
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as t:
        for name, body in (("compose.yaml", f"# compose for {ref}\n"),
                           ("scs-update", "#!/usr/bin/env bash\necho next updater\n"),
                           ("browser-icon.svg", "<svg/>\n")):
            data = body.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    sys.stdout.buffer.write(out.getvalue())
elif args[:2] == ["image", "rm"]:
    pass
else:
    sys.exit(f"fake docker: unexpected {args}")
'''


@pytest.fixture
def install(tmp_path):
    """An install folder with scs-update, running OLD, and a fake docker."""
    root = tmp_path / "scs"
    root.mkdir()
    (root / "logs").mkdir()
    shutil.copy(SCRIPT, root / "scs-update")
    (root / "compose.yaml").write_text("# compose for the old release\n")
    (root / ".env").write_text(f"SCS_CHANNEL=hub/scs:latest\nSCS_IMAGE={OLD}\nSCS_UID=1000\n"
                               f'SCS_PROFILE="{tmp_path}/profile dir"\n')
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "docker").write_text(FAKE_DOCKER)
    (bin_ / "docker").chmod(0o755)
    home = tmp_path / "docker"
    home.mkdir()

    class Install:
        path = root

        def state(self, **changes):
            path = home / "state.json"
            data = json.loads(path.read_text()) if path.exists() else {
                "policy": "enabled=1\ncheck_hours=24\nwait_for_browser=1",
                "latest": NEW, "running": OLD, "broken": [],
                "versions": {OLD: "1.0.0", NEW: "1.1.0"}}
            data.update(changes)
            path.write_text(json.dumps(data))
            return data

        def run(self, *args, browser_open=False):
            pgrep = bin_ / "pgrep"
            pgrep.write_text(f"#!/bin/sh\nexit {0 if browser_open else 1}\n")
            pgrep.chmod(0o755)
            env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "FAKE_DOCKER": str(home)}
            subprocess.run(["bash", str(root / "scs-update"), *args], env=env, cwd=tmp_path,
                           check=False, capture_output=True, timeout=60)

        def calls(self):
            path = home / "calls.jsonl"
            calls = [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []
            path.unlink(missing_ok=True)
            return calls

        def status(self):
            return json.loads((root / "logs" / "update.json").read_text())

        def env(self, key):
            for line in (root / ".env").read_text().splitlines():
                if line.startswith(key + "="):
                    return line.split("=", 1)[1]

        def update_state(self, key):
            path = root / ".update-state"
            for line in path.read_text().splitlines() if path.exists() else ():
                if line.startswith(key + "="):
                    return line.split("=", 1)[1]
            return None

    inst = Install()
    inst.state()
    return inst


def ran(calls, *prefix):
    return any(c[:len(prefix)] == list(prefix) for c in calls)


def test_applies_a_new_release(install):
    install.run()
    calls = install.calls()
    assert ran(calls, "pull", "-q", "hub/scs:latest") and ran(calls, "compose", "up", "-d")
    assert install.env("SCS_IMAGE") == NEW
    assert install.state()["running"] == NEW
    assert (install.path / "compose.yaml").read_text() == f"# compose for {NEW}\n"
    # The updater and icon are replaced once the release is healthy.
    assert "next updater" in (install.path / "scs-update").read_text()
    assert os.access(install.path / "scs-update", os.X_OK)
    assert (install.path / "browser-icon.svg").exists()
    assert ran(calls, "image", "rm", OLD)
    s = install.status()
    assert s["state"] == "updated" and s["version"] == "1.1.0" and "1.1.0" in s["message"]
    # Other .env lines are kept, quotes included.
    assert install.env("SCS_UID") == "1000" and install.env("SCS_PROFILE").startswith('"')
    assert not list(install.path.glob(".stage.*")) and not (install.path / ".update.lock").exists()


def test_up_to_date(install):
    install.state(latest=OLD)
    install.run()
    calls = install.calls()
    assert ran(calls, "pull") and not ran(calls, "compose", "up")
    assert install.status()["state"] == "up_to_date"
    assert install.env("SCS_IMAGE") == OLD


def test_checks_at_most_every_check_hours(install):
    install.state(latest=OLD)
    install.run()
    install.calls()
    install.run()
    assert not ran(install.calls(), "pull")
    install.run("--now")
    assert ran(install.calls(), "pull")


def test_broken_release_rolls_back_and_is_skipped(install):
    install.state(broken=[NEW])
    install.run()
    calls = install.calls()
    assert install.env("SCS_IMAGE") == OLD
    assert install.state()["running"] == OLD
    assert (install.path / "compose.yaml").read_text() == "# compose for the old release\n"
    assert "next updater" not in (install.path / "scs-update").read_text()
    assert not ran(calls, "image", "rm", OLD)
    s = install.status()
    assert s["state"] == "failed" and "1.0.0" in s["message"]
    assert install.update_state("BAD") == NEW
    assert "fell over" in (install.path / "update.log").read_text()
    # The same release isn't tried again; a newer one is.
    install.run("--now")
    calls = install.calls()
    assert not ran(calls, "compose", "up") and install.status()["state"] == "skipped"
    newer = "hub/scs@sha256:" + "c" * 64
    install.state(latest=newer, versions={OLD: "1.0.0", NEW: "1.1.0", newer: "1.1.1"})
    install.run("--now")
    calls = install.calls()
    assert install.env("SCS_IMAGE") == newer and ran(calls, "image", "rm", NEW)
    assert install.update_state("BAD") == ""


def test_waits_for_the_browser_to_close(install):
    install.run(browser_open=True)
    assert not ran(install.calls(), "compose", "up")
    assert install.status()["state"] == "pending"
    assert install.update_state("PENDING") == NEW
    # The next hourly run applies it without checking Docker Hub again.
    install.run()
    calls = install.calls()
    assert not ran(calls, "pull") and ran(calls, "compose", "up")
    assert install.env("SCS_IMAGE") == NEW


def test_applies_with_the_browser_open_when_told_to(install):
    install.state(policy="enabled=1\ncheck_hours=24\nwait_for_browser=0")
    install.run(browser_open=True)
    assert install.env("SCS_IMAGE") == NEW


def test_disabled(install):
    install.state(policy="enabled=0\ncheck_hours=24\nwait_for_browser=1")
    install.run()
    assert not ran(install.calls(), "pull")
    assert install.status()["state"] == "disabled"


def test_offline(install):
    install.state(offline=True)
    install.run()
    assert install.status()["state"] == "error"
    assert install.env("SCS_IMAGE") == OLD
    # Not counted as a check, so the next run tries again.
    assert install.update_state("LAST_CHECK") is None


def test_policy_is_never_evaluated(install, tmp_path):
    marker = tmp_path / "pwned"
    install.state(policy=f"enabled=1\ncheck_hours=$(touch {marker})\n`touch {marker}`")
    install.run()
    assert not marker.exists()
    assert install.env("SCS_IMAGE") == NEW


def test_status_replaces_a_planted_symlink(install, tmp_path):
    target = tmp_path / "victim"
    target.write_text("keep\n")
    (install.path / "logs" / "update.json").symlink_to(target)
    install.state(latest=OLD)
    install.run()
    assert target.read_text() == "keep\n"
    assert not (install.path / "logs" / "update.json").is_symlink()
