import http.client
import json
import socket

import pytest

from censor_proxy import admin as admin_mod
from censor_proxy.admin import AdminServer, PinLock, UserConfigFile, defaults_help, hash_pin, verify_pin
from censor_proxy.config import load_config
from censor_proxy.video import Job, VideoJobs

USER_CONFIG = """\
# My overrides.

censor:
  style: gaussian          # gaussian | pixelate | solid

proxy:
  # Pinned clients.
  passthrough_hosts:
    - '^update\\.googleapis\\.com:'
"""


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeApp:
    def __init__(self, cfg, jobs=None):
        self.cfg = cfg
        self.jobs = jobs
        self.applied = []

    def status(self):
        return {"ready": True, "counts": {"censored": 3}}

    def apply_config(self, cfg):
        self.applied.append(cfg)
        self.cfg = cfg


@pytest.fixture
def panel(tmp_path):
    port = free_port()
    path = tmp_path / "config.yaml"
    path.write_text(USER_CONFIG + f"\nadmin:\n  bind: 127.0.0.1\n  port: {port}\n"
                    f"video:\n  cache_dir: '{tmp_path / 'video'}'\n"
                    f"logging:\n  path: '{tmp_path / 'detections.jsonl'}'\n")
    cfg = load_config(path)
    app = FakeApp(cfg, VideoJobs(cfg))
    server = AdminServer(cfg, app, UserConfigFile(path))
    server.start()
    yield Client(port), app, server, path
    server.stop()


class Client:
    def __init__(self, port):
        self.port = port
        self.cookie = None

    def __call__(self, method, path, body=None, host=None, origin="same", headers=None):
        host = host or f"localhost:{self.port}"
        hdrs = {"Host": host, **(headers or {})}
        if origin == "same":
            origin = f"https://{host}" if host == "scs.local" else f"http://{host}"
        if origin and method != "GET":
            hdrs["Origin"] = origin
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        if self.cookie:
            hdrs["Cookie"] = self.cookie
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=data, headers=hdrs)
        r = conn.getresponse()
        raw = r.read()
        if cookie := r.getheader("Set-Cookie"):
            self.cookie = cookie.split(";", 1)[0] if "Max-Age=0" not in cookie else None
        conn.close()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = raw
        return r, payload


def test_panel_served_on_allowed_hosts_only(panel):
    client, *_ = panel
    r, body = client("GET", "/admin/")
    assert r.status == 200 and b"admin.js" in body
    assert "frame-ancestors 'none'" in r.getheader("Content-Security-Policy")
    assert r.getheader("Access-Control-Allow-Origin") is None
    assert client("GET", "/admin/", host="scs.local")[0].status == 200
    assert client("GET", "/admin/admin.js")[0].getheader("Content-Type").startswith("text/javascript")
    # A DNS-rebinding page arrives with its own name.
    assert client("GET", "/admin/api/status", host=f"evil.example:{client.port}")[0].status == 421


def test_cross_site_requests_refused(panel):
    client, app, _, path = panel
    before = path.read_text()
    change = {"set": {"censor.style": "solid"}}
    assert client("POST", "/admin/api/config", change, origin="https://evil.example")[0].status == 403
    assert client("POST", "/admin/api/config", change, origin=None)[0].status == 403
    # A form or text/plain post (no preflight) is refused too.
    r, _ = client("POST", "/admin/api/config", change, headers={"Content-Type": "text/plain"})
    assert r.status == 415
    r, _ = client("GET", "/admin/api/status", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status == 403
    assert path.read_text() == before and not app.applied


def test_settings_saved_with_comments_and_applied(panel):
    client, app, _, path = panel
    r, view = client("POST", "/admin/api/config", {"set": {
        "censor.style": "pixelate",
        "whitelist.domains": ["youtube.com", "https://Example.ORG/page", "youtube.com"],
        "detection.threshold": 0.35,
    }})
    assert r.status == 200, view
    text = path.read_text()
    assert "# My overrides." in text and "# Pinned clients." in text
    assert "style: pixelate          # gaussian | pixelate | solid" in text
    assert "- youtube.com\n    - example.org\n" in text
    assert app.cfg.censor.style == "pixelate"
    assert app.cfg.whitelist.domains == ("youtube.com", "example.org")
    assert app.cfg.detection.threshold == 0.35
    style = next(k for s in view["sections"] if s["name"] == "censor" for k in s["keys"]
                 if k["key"] == "style")
    assert style["value"] == "pixelate" and style["overridden"]

    # Resetting, or setting the default value, takes the key out of the file.
    client("POST", "/admin/api/config", {"set": {"detection.threshold": 0.3},
                                          "reset": ["censor.style"]})
    text = path.read_text()
    assert "pixelate" not in text and "threshold" not in text
    assert app.cfg.censor.style == "gaussian"


@pytest.mark.parametrize("change", [
    {"set": {"censor.style": "sparkles"}},
    {"set": {"censor.padding": "lots"}},
    {"set": {"model.tiling": 1}},
    {"set": {"censor.stlye": "solid"}},
    {"set": {"whitelist.domains": ["not a domain"]}},
    {"set": {"admin.pin_hash": "x"}},
    {"reset": ["admin.pin_hash"]},
])
def test_bad_settings_rejected(panel, change):
    client, app, _, path = panel
    before = path.read_text()
    r, body = client("POST", "/admin/api/config", change)
    assert r.status == 400 and body["error"]
    assert path.read_text() == before and not app.applied


def test_failed_apply_leaves_file_alone(panel):
    client, app, _, path = panel
    before = path.read_text()

    def broken(cfg):
        raise RuntimeError("model file missing")
    app.apply_config = broken
    r, body = client("POST", "/admin/api/config", {"set": {"model.path": "/nope.onnx"}})
    assert r.status == 400 and "model file missing" in body["error"]
    assert path.read_text() == before


def test_restart_only_settings_marked(panel):
    client, *_ = panel
    _, view = client("POST", "/admin/api/config", {"set": {"video.max_concurrent_jobs": 3}})
    key = next(k for s in view["sections"] if s["name"] == "video" for k in s["keys"]
               if k["key"] == "max_concurrent_jobs")
    assert key["restart"] and key["pending_restart"]
    assert all(k["key"] != "pin_hash" for s in view["sections"] for k in s["keys"])


def test_reload_from_file(panel):
    client, app, _, path = panel
    path.write_text(path.read_text().replace("style: gaussian", "style: solid"))
    r, _ = client("POST", "/admin/api/config/reload", {})
    assert r.status == 200 and app.cfg.censor.style == "solid"


def test_pin_lock(panel):
    client, app, server, path = panel
    assert client("GET", "/admin/api/session")[1] == {"pin_set": False, "unlocked": True,
                                                      "retry_after": 0}
    r, body = client("POST", "/admin/api/pin", {"new": "12ab"})
    assert r.status == 400
    r, body = client("POST", "/admin/api/pin", {"new": "4321"})
    assert r.status == 200 and body["pin_set"] and client.cookie
    assert "4321" not in path.read_text() and "pin_hash: pbkdf2_sha256$" in path.read_text()
    assert app.cfg.admin.pin_hash
    assert client("GET", "/admin/api/status")[0].status == 200  # the setter stays in

    client.cookie = None
    r, body = client("GET", "/admin/api/status")
    assert r.status == 401 and body["locked"]
    assert client("GET", "/admin/")[0].status == 200  # the page itself shows the PIN prompt
    r, body = client("POST", "/admin/api/unlock", {"pin": "0000"})
    assert r.status == 403 and body["attempts_left"] == 4
    r, body = client("POST", "/admin/api/unlock", {"pin": "4321"})
    assert r.status == 200 and client.cookie
    assert client("GET", "/admin/api/status")[0].status == 200

    # Settings can't be changed while locked.
    client("POST", "/admin/api/lock", {})
    assert client.cookie is None
    assert client("POST", "/admin/api/config", {"set": {"censor.style": "solid"}})[0].status == 401
    # Removing needs the current PIN.
    client("POST", "/admin/api/unlock", {"pin": "4321"})
    assert client("POST", "/admin/api/pin", {"current": "1111", "new": None})[0].status == 403
    r, body = client("POST", "/admin/api/pin", {"current": "4321", "new": None})
    assert r.status == 200 and not body["pin_set"]
    assert "pin_hash" not in path.read_text()
    client.cookie = None
    assert client("GET", "/admin/api/status")[0].status == 200


def test_pin_lockout_and_expiry(make_config):
    now = [1000.0]
    cfg = make_config("admin: {pin_max_attempts: 3, pin_lockout_seconds: 60, session_seconds: 30}\n")
    lock = PinLock(cfg.admin, clock=lambda: now[0])
    lock.cfg = type(cfg.admin)(**{**cfg.admin.__dict__, "pin_hash": hash_pin("2468")})
    for left in (2, 1):
        with pytest.raises(admin_mod.AdminError) as e:
            lock.check("1111")
        assert e.value.status == 403 and e.value.body["attempts_left"] == left
    with pytest.raises(admin_mod.AdminError) as e:
        lock.check("1111")
    assert e.value.status == 429 and e.value.body["retry_after"] == 60
    with pytest.raises(admin_mod.AdminError) as e:
        lock.check("2468")  # even the right PIN waits out the lockout
    assert e.value.status == 429
    now[0] += 61
    lock.check("2468")
    token = lock.open_session()
    now[0] += 20
    assert lock.valid(token, touch=False)
    now[0] += 20  # 40 s since last use: background polling didn't count
    assert not lock.valid(token)


def test_pin_hash():
    stored = hash_pin("123456")
    assert verify_pin("123456", stored) and not verify_pin("123457", stored)
    assert stored != hash_pin("123456")  # salted
    assert not verify_pin("123456", "garbage")


def test_log_tail(panel):
    client, app, *_ = panel
    log = app.cfg.logging.path
    with open(log, "w") as f:
        for i in range(10):
            f.write(json.dumps({"ts": str(i), "host": f"h{i}", "action": "clean"}) + "\n")
        f.write("not json\n")
    r, body = client("GET", "/admin/api/log?limit=3")
    assert r.status == 200
    assert [e["host"] for e in body["entries"]] == ["h9", "h8", "h7"]


def test_jobs_listed_and_cancelled(panel):
    client, app, *_ = panel
    job = Job("a" * 24, "https://cdn.example/v.mp4", "https://site.example/page", state="running")
    app.jobs._jobs[job.id] = job
    r, body = client("GET", "/admin/api/jobs")
    assert r.status == 200
    assert body["jobs"][0]["src"] == "https://cdn.example/v.mp4"
    assert body["jobs"][0]["page"] == "https://site.example/page"
    assert client("POST", f"/admin/api/jobs/{job.id}/cancel", {})[0].status == 200
    assert job.cancel_reason == "cancelled from the admin panel"
    assert client("POST", f"/admin/api/jobs/{'b' * 24}/cancel", {})[0].status == 404


def test_config_file_missing_or_unset(tmp_path, make_config):
    cfg = make_config()
    with pytest.raises(admin_mod.AdminError) as e:
        UserConfigFile(None).prepare({"censor.style": "solid"}, [])
    assert e.value.status == 409
    # A missing file is created on first save.
    f = UserConfigFile(tmp_path / "new.yaml")
    doc, cfg = f.prepare({"whitelist.domains": ["youtube.com"]}, [])
    f.write(doc)
    assert load_config(tmp_path / "new.yaml").whitelist.domains == ("youtube.com",)


def test_help_text_from_defaults():
    help_ = defaults_help()
    assert "subdomains" in help_["whitelist"]["domains"]
    assert help_["censor"]["style"] == "gaussian | pixelate | solid"
