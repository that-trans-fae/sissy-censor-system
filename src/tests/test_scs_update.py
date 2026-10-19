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


def kept(image):
    return image.replace("@sha256:", ":sha256-")

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
    policies = state["policy"] if isinstance(state["policy"], list) else [state["policy"]]
    asked = state.get("policy_reads", 0)
    print(policies[min(asked, len(policies) - 1)])
    state["policy_reads"] = asked + 1
    (home / "state.json").write_text(json.dumps(state))
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
        updater = state["updaters"].get(ref) or Path(os.environ["REAL_UPDATER"]).read_text()
        for name, body in (("compose.yaml", f"# compose for {ref}\n"),
                           ("compose.vaapi.yaml", f"# vaapi for {ref}\n"),
                           ("scs-update", updater),
                           ("browser-icon.svg", "<svg/>\n")):
            data = body.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    sys.stdout.buffer.write(out.getvalue())
elif args[:2] == ["image", "rm"] or args[:1] == ["tag"]:
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
    (root / "compose.vaapi.yaml").write_text("# vaapi for the old release\n")
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
                "policy": "enabled=1\nwait_for_browser=1",
                "latest": NEW, "running": OLD, "broken": [],
                "versions": {OLD: "1.0.0", NEW: "1.1.0"},
                # The updater each release ships; the others ship this one.
                "updaters": {NEW: "#!/usr/bin/env bash\necho next updater\n"}}
            data.update(changes)
            path.write_text(json.dumps(data))
            return data

        def run(self, *args, browser_open=0):
            """browser_open: how many times pgrep finds the browser before it closes."""
            pgrep = bin_ / "pgrep"
            count = tmp_path / "pgrep-count"
            count.write_text("0")
            pgrep.write_text(f"#!/bin/sh\nn=$(cat {count}); echo $((n + 1)) >{count}\n"
                             f"[ \"$n\" -lt {int(browser_open)} ]\n")
            pgrep.chmod(0o755)
            self.browser_checks = lambda: int(count.read_text())
            env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "FAKE_DOCKER": str(home),
                   "SCS_UPDATE_POLL": "0", "SCS_UPDATE_PULL_TRIES": "3", "REAL_UPDATER": str(SCRIPT)}
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
    # Its overrides too (COMPOSE_FILE may name them), in place before the container is recreated.
    assert (install.path / "compose.vaapi.yaml").read_text() == f"# vaapi for {NEW}\n"
    assert not list(install.path.glob(".compose*.previous"))
    # The updater and icon are replaced once the release is healthy.
    assert "next updater" in (install.path / "scs-update").read_text()
    assert os.access(install.path / "scs-update", os.X_OK)
    assert (install.path / "browser-icon.svg").exists()
    # The new release is kept by a local tag; the old one's is dropped.
    assert ran(calls, "tag", NEW, kept(NEW)) and ran(calls, "image", "rm", kept(OLD))
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


def test_up_to_date_finishes_a_cut_off_update(install):
    # A run stopped after recreating the container on NEW (pinned in .env)
    # but before moving the new updater and icon in.
    env = (install.path / ".env").read_text().replace(OLD, NEW)
    (install.path / ".env").write_text(env)
    (install.path / ".compose.yaml.previous").write_text("# compose for the old release\n")
    install.state(running=NEW)
    install.run()
    calls = install.calls()
    assert not ran(calls, "compose", "up")
    assert "next updater" in (install.path / "scs-update").read_text()
    assert os.access(install.path / "scs-update", os.X_OK)
    assert (install.path / "browser-icon.svg").exists()
    assert not list(install.path.glob(".compose*.previous")) and not list(install.path.glob(".stage.*"))
    assert install.status()["state"] == "up_to_date"


def test_up_to_date_leaves_matching_files(install):
    install.state(latest=OLD)
    install.run()
    install.calls()
    before = (install.path / "scs-update").stat().st_ino
    install.run()
    assert (install.path / "scs-update").stat().st_ino == before


def test_checks_on_every_run(install):
    install.state(latest=OLD)
    install.run()
    install.calls()
    install.run()
    assert ran(install.calls(), "pull")


def test_broken_release_rolls_back_and_is_skipped(install):
    install.state(broken=[NEW])
    install.run()
    calls = install.calls()
    assert install.env("SCS_IMAGE") == OLD
    assert install.state()["running"] == OLD
    assert (install.path / "compose.yaml").read_text() == "# compose for the old release\n"
    assert (install.path / "compose.vaapi.yaml").read_text() == "# vaapi for the old release\n"
    assert not list(install.path.glob(".compose*.previous"))
    assert "next updater" not in (install.path / "scs-update").read_text()
    assert not ran(calls, "image", "rm", kept(OLD)) and not ran(calls, "image", "rm", kept(NEW))
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
    assert install.env("SCS_IMAGE") == newer and ran(calls, "image", "rm", kept(NEW))
    assert install.update_state("BAD") == ""


def test_waits_for_the_browser_to_close(install):
    # The same run waits while the browser is open, then applies it.
    install.run(browser_open=3)
    calls = install.calls()
    assert install.browser_checks() == 4
    assert sum(c[:1] == ["pull"] for c in calls) == 1 and ran(calls, "compose", "up")
    assert install.env("SCS_IMAGE") == NEW
    assert "pending: Release 1.1.0 is downloaded" in (install.path / "update.log").read_text()
    assert install.status()["state"] == "updated"


def test_turned_off_while_waiting_for_the_browser(install):
    install.state(policy=["enabled=1\nwait_for_browser=1", "enabled=0\nwait_for_browser=1"])
    install.run(browser_open=2)
    assert not ran(install.calls(), "compose", "up")
    assert install.env("SCS_IMAGE") == OLD
    s = install.status()
    assert s["state"] == "disabled" and "1.1.0 wasn't installed" in s["message"]


def test_applies_with_the_browser_open_when_told_to(install):
    install.state(policy="enabled=1\nwait_for_browser=0")
    install.run(browser_open=99)
    assert install.env("SCS_IMAGE") == NEW
    assert install.browser_checks() == 0


def test_now_applies_with_the_browser_open(install):
    install.run("--now", browser_open=99)
    assert install.env("SCS_IMAGE") == NEW


def test_disabled(install):
    install.state(policy="enabled=0\nwait_for_browser=1")
    install.run()
    assert not ran(install.calls(), "pull")
    assert install.status()["state"] == "disabled"


def test_offline(install):
    install.state(offline=True)
    install.run()
    calls = install.calls()
    # Tried SCS_UPDATE_PULL_TRIES times, a poll apart.
    assert sum(c[:1] == ["pull"] for c in calls) == 3
    s = install.status()
    assert s["state"] == "error" and "next startup" in s["message"]
    assert install.env("SCS_IMAGE") == OLD


def test_policy_is_never_evaluated(install, tmp_path):
    marker = tmp_path / "pwned"
    install.state(policy=f"enabled=$(touch {marker})\nwait_for_browser=`touch {marker}`\n$(touch {marker})")
    install.run()
    assert not marker.exists()
    assert install.env("SCS_IMAGE") == NEW


def test_one_run_at_a_time(install):
    lock = install.path / ".update.lock"
    lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))
    install.run()
    assert not ran(install.calls(), "pull")
    # A lock left by a run that is gone is taken over.
    (lock / "pid").write_text("999999999")
    install.run()
    assert ran(install.calls(), "pull") and not lock.exists()


def test_status_replaces_a_planted_symlink(install, tmp_path):
    target = tmp_path / "victim"
    target.write_text("keep\n")
    (install.path / "logs" / "update.json").symlink_to(target)
    install.state(latest=OLD)
    install.run()
    assert target.read_text() == "keep\n"
    assert not (install.path / "logs" / "update.json").is_symlink()
