"""Admin panel: status, the detection log, video jobs, the whitelist and
settings, with an optional PIN lock.

Reached two ways, both ending at this listener (admin.port):

  http://localhost:<port>/admin   compose publishes the port on the host's loopback
  https://scs.local/admin         through the proxy, which routes proxy.admin_host here

Any page you browse can send requests to both, so:
  - the Host must be proxy.admin_host or one of admin.allowed_hosts, which
    shuts out DNS rebinding;
  - writes must be JSON with the panel's own Origin. Nothing answers with
    CORS headers, so a cross-site page can't read responses, and can't send
    JSON without a preflight that is never approved;
  - the panel can't be framed.

  GET  /admin/                     the panel
  GET  /admin/api/session          {pin_set, unlocked}
  POST /admin/api/unlock           {pin}
  POST /admin/api/lock
  POST /admin/api/pin              {current, new}: set, change or (new: null) remove
  GET  /admin/api/attempts         wrong PIN entries not yet dismissed
  POST /admin/api/attempts/ack     dismiss them

With a PIN set, everything but the page itself, session and unlock answers
401 until unlocked. Wrong PINs are never locked out; they are recorded
(admin.attempts_log) and reported after the next unlock.
  GET  /admin/api/status          including the version and scs-update's last result
  GET  /admin/api/log
  GET  /admin/api/jobs
  POST /admin/api/jobs/<id>/cancel
  GET  /admin/api/config
  POST /admin/api/config           {set: {"section.key": value}, reset: ["section.key"]}
  POST /admin/api/config/reload    re-read config.yaml (after editing it by hand)

Requests with "X-Admin-Background: 1" (the panel's auto-refresh) don't count
as use, so an open panel still locks after admin.session_seconds.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import threading
import time
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Protocol
from urllib.parse import parse_qs, urlsplit

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from . import config, updates
from .config import AdminConfig, Config, ConfigError
from .video import Job, VideoJobs

log = logging.getLogger("censor_proxy.admin")

STATIC = Path(__file__).with_name("static")
STATIC_FILES = {"admin.js": "text/javascript; charset=utf-8",
                "admin.css": "text/css; charset=utf-8"}
CANCEL_PATH = re.compile(r"^jobs/([0-9a-f]{24})/cancel$")
# Settings that are read once at startup.
RESTART_KEYS = frozenset({"proxy.internal_port", "admin.enabled", "admin.bind", "admin.port",
                          "video.cache_dir"})
# Managed through their own endpoints, never shown or set as plain settings.
HIDDEN_KEYS = frozenset({"admin.pin_hash"})
# Settable, but edited in config.yaml rather than shown as fields: the panel
# offers them as choices (the censoring level, the model list).
NOT_SHOWN_KEYS = frozenset({"detection.presets", "model.available", "model.resource_levels"})
# The keys a resource level sets (model.resource_levels), each with its
# field in a level.
RESOURCE_KEYS = {"model.workers": "workers", "model.threads_per_worker": "threads_per_worker",
                 "video.max_concurrent_jobs": "video_jobs"}
NULLABLE_KEYS = frozenset({"logging.path", "detection.label_thresholds", "admin.attempts_log",
                           "video_attribution.model_path"})
PIN = re.compile(r"^\d{4,12}$")
COOKIE = "scs_admin"
PBKDF2_ITERATIONS = 200_000
MAX_BODY = 65536
LOG_TAIL_BYTES = 8 * 1024 * 1024
# Body parts as a censoring level lists them; others are named from the label.
PART_NAMES = {"FEMALE_BREAST": "breasts", "MALE_BREAST": "male chests",
              "FEMALE_GENITALIA": "vaginas", "MALE_GENITALIA": "penises",
              "ARMPITS": "armpits", "FEET": "feet"}


def label_name(label: str) -> str:
    """A NudeNet label for people: FEMALE_BREAST_EXPOSED -> Exposed breasts."""
    words = str(label).upper().split("_")
    state = words.pop().capitalize() if words and words[-1] in ("EXPOSED", "COVERED") else ""
    part = (PART_NAMES.get("_".join(words))
            or " ".join(w.lower() for w in words if w not in ("FEMALE", "MALE")))
    return f"{state} {part}".strip() if state else part.capitalize()


def preset_censors(preset: dict | None, base: dict) -> list[str]:
    """What a preset censors: its female and unsexed labels, named. Unsexed
    labels that pass next to a man say so."""
    d = {**base, **((preset or {}).get("settings") or {})}
    names = [label_name(l) for l in d.get("female_labels") or ()]
    exempt = ", except next to a man" if d.get("male_context_exempts") else ""
    names += [label_name(l) + exempt for l in d.get("unsexed_labels") or ()]
    return list(dict.fromkeys(names))


class App(Protocol):
    """What the panel needs from the running proxy (the addon)."""
    cfg: Config
    jobs: VideoJobs | None

    def status(self) -> dict: ...

    def apply_config(self, cfg: Config) -> None: ...


class AdminError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.body = {"error": message, **extra}


# -- PIN ------------------------------------------------------------------

def hash_pin(pin: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, PBKDF2_ITERATIONS)
    return "$".join(("pbkdf2_sha256", str(PBKDF2_ITERATIONS),
                     base64.b64encode(salt).decode(), base64.b64encode(digest).decode()))


def verify_pin(pin: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", pin.encode(), base64.b64decode(salt), int(iterations))
        return hmac.compare_digest(got, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


class PinLock:
    """Unlocked sessions, in memory: a restart locks the panel again."""

    def __init__(self, cfg: AdminConfig, clock=time.monotonic):
        self.cfg = cfg
        self._clock = clock
        self._sessions: dict[str, float] = {}  # token -> last use
        self._lock = threading.Lock()

    @property
    def pin_set(self) -> bool:
        return bool(self.cfg.pin_hash)

    def valid(self, token: str | None, touch: bool = True) -> bool:
        if not self.pin_set:
            return True
        with self._lock:
            now = self._clock()
            for t, last in list(self._sessions.items()):
                if now - last > self.cfg.session_seconds:
                    del self._sessions[t]
            if token not in self._sessions:
                return False
            if touch:
                self._sessions[token] = now
            return True

    def check(self, pin: str) -> bool:
        """Whether `pin` is right. Checks are serialised and slow (PBKDF2),
        which paces guessing without ever locking anyone out."""
        with self._lock:
            return verify_pin(str(pin), self.cfg.pin_hash or "")

    def open_session(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = self._clock()
        return token

    def close(self, token: str | None) -> None:
        with self._lock:
            self._sessions.pop(token or "", None)

    def close_all(self) -> None:
        with self._lock:
            self._sessions.clear()


class AttemptLog:
    """Wrong PIN entries, kept until dismissed in the panel. Appended to
    admin.attempts_log (JSONL) so a restart doesn't hide them; entries that
    can't be written are kept in memory."""

    def __init__(self, path: str | None):
        self.path = Path(path) if path else None
        self._memory: list[dict] = []
        self._lock = threading.Lock()

    def _append(self, entry: dict) -> None:
        with self._lock:
            if self.path:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self.path.open("a") as f:
                        f.write(json.dumps(entry, separators=(",", ":")) + "\n")
                    return
                except OSError as e:
                    log.warning("admin: can't write %s: %s", self.path, e)
            self._memory.append(entry)

    def record(self, action: str, via: str, user_agent: str | None) -> None:
        log.warning("admin: wrong PIN (%s, via %s)", action, via)
        self._append({"ts": time.time(), "event": "wrong_pin", "action": action, "via": via,
                      "user_agent": (user_agent or "")[:300]})

    def acknowledge(self) -> None:
        self._append({"ts": time.time(), "event": "acknowledged"})

    def unacknowledged(self) -> list[dict]:
        """Wrong PIN entries since the last dismissal, oldest first."""
        entries: list[dict] = []
        if self.path and self.path.is_file():
            with self._lock, self.path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                start = max(0, f.tell() - LOG_TAIL_BYTES)
                f.seek(start)
                lines = f.read().splitlines()[1 if start else 0:]
            for line in lines:
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
        with self._lock:
            entries = sorted(entries + self._memory, key=lambda e: e.get("ts", 0))
        pending: list[dict] = []
        for e in entries:
            if e.get("event") == "acknowledged":
                pending = []
            elif e.get("event") == "wrong_pin":
                pending.append(e)
        return pending


# -- the user config file ---------------------------------------------------

def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)  # the style config.yaml is written in
    y.width = 4096
    return y


def _plain(value: Any) -> Any:
    """ruamel's round-trip types as plain Python."""
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    for t in (int, float, str):
        if isinstance(value, t):
            return t(value)
    return value


def _check_type(dotted: str, value: Any, default: Any) -> None:
    if value is None:
        ok = default is None or dotted in NULLABLE_KEYS
    elif default is None:
        ok = isinstance(value, str)
    elif isinstance(default, bool):
        ok = isinstance(value, bool)
    elif isinstance(default, (int, float)):
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    else:
        ok = isinstance(value, type(default))
    if not ok:
        raise AdminError(400, f"{dotted}: expected {type(default).__name__}, "
                              f"got {type(value).__name__}")


class UserConfigFile:
    """The user config (config.yaml), edited in place with its comments kept."""

    def __init__(self, path: Path | None):
        self.path = path

    @property
    def editable(self) -> bool:
        if not self.path:
            return False
        if self.path.exists():
            return os.access(self.path, os.W_OK) and os.access(self.path.parent, os.W_OK)
        return self.path.parent.is_dir() and os.access(self.path.parent, os.W_OK)

    def read(self) -> CommentedMap:
        if not self.path or not self.path.is_file():
            return CommentedMap()
        doc = _yaml().load(self.path.read_text())
        if doc is None:
            return CommentedMap()
        if not isinstance(doc, dict):
            raise ConfigError(f"{self.path}: top level must be a mapping")
        return doc

    def override(self) -> dict:
        return _plain(self.read())

    def prepare(self, sets: dict[str, Any], resets: Iterable[str],
                allow_hidden: bool = False) -> tuple[CommentedMap, Config]:
        """Apply changes to a copy of the file and validate the result.
        Values equal to their default are removed rather than written."""
        if not self.path:
            raise AdminError(409, "No config file is set (CENSOR_CONFIG), so nothing can be saved.")
        defaults = config.defaults()
        doc = self.read()
        before = self._preset(doc)
        before_resources = self._resources(doc)
        for dotted, value in sets.items():
            section, key = _split(dotted, defaults, allow_hidden)
            _check_type(dotted, value, defaults[section][key])
            if dotted == "whitelist.domains":
                value = list(dict.fromkeys(config.normalize_domain(d) for d in value if str(d).strip()))
            if value == defaults[section][key] and type(value) is type(defaults[section][key]):
                _remove(doc, section, key)
            else:
                if section not in doc or not isinstance(doc[section], dict):
                    doc[section] = CommentedMap()
                doc[section][key] = value
        for dotted in resets:
            _remove(doc, *_split(dotted, defaults, allow_hidden))
        self._check_preset(doc, before, [*sets, *resets], defaults)
        self._check_resources(doc, before_resources, [*sets, *resets], defaults)
        try:
            cfg = config.from_override(_plain(doc))
        except ConfigError as e:
            raise AdminError(400, str(e)) from e
        return doc, cfg

    @staticmethod
    def _preset(doc: CommentedMap) -> tuple[str, dict]:
        """(preset name, presets) as the file has them, before applying."""
        try:
            d = config.merged(_plain(doc), apply_preset=False)["detection"]
        except ConfigError as e:
            raise AdminError(400, str(e)) from e
        return d["preset"], d["presets"] or {}

    def _check_preset(self, doc: CommentedMap, before: tuple[str, dict],
                      changed: list[str], defaults: dict) -> None:
        """Detection keys are the preset's while one is chosen. Switching to
        custom with no custom keys saved starts from the previous preset."""
        name, presets = self._preset(doc)
        detection_keys = [k for k in changed if k.startswith("detection.")
                          and k.split(".", 1)[1] not in config.PRESET_KEYS]
        if name != config.CUSTOM_PRESET:
            if detection_keys:
                raise AdminError(400, f"Detection settings come from the {name} level. "
                                      "Choose Custom to set them yourself.")
            return
        old, old_presets = before
        saved = doc.get("detection")
        if (old == config.CUSTOM_PRESET or not isinstance(saved, dict)
                or any(k not in config.PRESET_KEYS for k in saved)):
            return
        for key, value in ((old_presets.get(old) or {}).get("settings") or {}).items():
            if key not in detection_keys and value != defaults["detection"][key]:
                doc["detection"][key] = value

    @staticmethod
    def _resources(doc: CommentedMap) -> tuple[str, dict]:
        """(resource level name, levels) as the file has them."""
        try:
            m = config.merged(_plain(doc), apply_preset=False)["model"]
        except ConfigError as e:
            raise AdminError(400, str(e)) from e
        levels = m["resource_levels"]
        return str(m["resources"]), levels if isinstance(levels, dict) else {}

    def _check_resources(self, doc: CommentedMap, before: tuple[str, dict],
                         changed: list[str], defaults: dict) -> None:
        """Workers, threads and video jobs are the level's while one is
        chosen (video jobs only if the level sets them). Switching to custom
        with none of them saved starts from the previous level."""
        name, levels = self._resources(doc)
        level = levels.get(name) or {}
        if name != config.CUSTOM_RESOURCES:
            if any(k in RESOURCE_KEYS and level.get(RESOURCE_KEYS[k]) is not None for k in changed):
                raise AdminError(400, f"Workers, threads and video jobs come from the "
                                      f"{level.get('label') or name} resource level. Choose "
                                      "Custom to set them yourself.")
            return
        old, old_levels = before
        keys = [k.split(".") for k in RESOURCE_KEYS]
        if old == config.CUSTOM_RESOURCES or any(isinstance(doc.get(s), dict) and k in doc[s]
                                                 for s, k in keys):
            return
        for section, key in keys:
            value = (old_levels.get(old) or {}).get(RESOURCE_KEYS[f"{section}.{key}"])
            if f"{section}.{key}" not in changed and value is not None \
                    and value != defaults[section][key]:
                if not isinstance(doc.get(section), dict):
                    doc[section] = CommentedMap()
                doc[section][key] = value

    def write(self, doc: CommentedMap) -> None:
        buf = io.StringIO()
        if doc or doc.ca.comment:
            _yaml().dump(doc, buf)
        text = buf.getvalue()
        if text.strip() == "{}":
            text = ""
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        try:
            tmp.write_text(text)
            if self.path.exists():
                st = self.path.stat()
                os.chmod(tmp, st.st_mode & 0o7777)
                try:
                    os.chown(tmp, st.st_uid, st.st_gid)
                except OSError:
                    pass
            try:
                os.replace(tmp, self.path)
            except OSError:  # a file bind-mounted on its own can't be replaced
                self.path.write_text(text)
                tmp.unlink(missing_ok=True)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            raise AdminError(409, f"Can't write {self.path}: {e.strerror}. "
                                  "Is /config mounted read-only?") from e


def _split(dotted: str, defaults: dict, allow_hidden: bool = False) -> tuple[str, str]:
    section, _, key = str(dotted).partition(".")
    if section not in defaults or key not in defaults[section]:
        raise AdminError(400, f"unknown setting: {dotted}")
    if dotted in HIDDEN_KEYS and not allow_hidden:
        raise AdminError(400, f"{dotted} can't be set here")
    return section, key


def _remove(doc: CommentedMap, section: str, key: str) -> None:
    if isinstance(doc.get(section), dict) and key in doc[section]:
        del doc[section][key]
        if not doc[section]:
            del doc[section]


def defaults_help() -> dict[str, dict[str, str]]:
    """The comment above each key in defaults.yaml, by section and key."""
    help_: dict[str, dict[str, str]] = {}
    section, pending = None, []
    for line in config.DEFAULTS_PATH.read_text().splitlines():
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if stripped.startswith("#"):
            pending.append(stripped[1:].strip())
            continue
        if not stripped:
            pending = []
            continue
        if indent == 0 and stripped.endswith(":"):
            section = stripped[:-1]
            help_[section] = {}
        elif indent == 2 and section and ":" in stripped and pending:
            help_[section][stripped.split(":", 1)[0]] = " ".join(pending)
        pending = []
    return help_


# -- the detection log ------------------------------------------------------

def read_log_tail(path: str | None, limit: int) -> list[dict]:
    """The newest `limit` entries, newest first, from the log and its first
    rotated backup."""
    entries: list[dict] = []
    for p in (Path(path), Path(f"{path}.1")) if path else ():
        if len(entries) >= limit or not p.is_file():
            break
        with p.open("rb") as f:
            f.seek(0, os.SEEK_END)
            start = max(0, f.tell() - LOG_TAIL_BYTES)
            f.seek(start)
            lines = f.read().splitlines()
        if start:
            lines = lines[1:]  # partial
        for line in reversed(lines):
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
            if len(entries) >= limit:
                break
    return entries


def job_view(job: Job, ready_seconds: float) -> dict:
    return {**job.status(ready_seconds), "src": job.src, "page": job.page or job.referer,
            "created": job.created, "finished": job.finished,
            "cancel_reason": job.cancel_reason, "last_seen": job.last_seen}


# -- the server -------------------------------------------------------------

class AdminServer:
    def __init__(self, cfg: Config, app: App, config_file: UserConfigFile,
                 update_status: Path = updates.STATUS_FILE):
        self.cfg = cfg
        self.app = app
        self.file = config_file
        self.update_status = update_status
        self.pin = PinLock(cfg.admin)
        self.attempts = AttemptLog(cfg.admin.attempts_log)
        # Startup values, to tell which restart-only settings are pending.
        try:
            self.boot = config.merged(config_file.override())
        except ConfigError:
            self.boot = config.defaults()
        self._apply_lock = threading.Lock()
        handler = type("Handler", (_Handler,), {"admin": self})
        self.httpd = ThreadingHTTPServer((cfg.admin.bind, cfg.admin.port), handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="censor-admin",
                                       daemon=True)

    @property
    def port(self) -> int:
        """The port listened on, fixed at startup whatever admin.port says now."""
        return self.httpd.server_address[1]

    def start(self) -> None:
        self.thread.start()
        log.info("admin panel on port %d (https://%s/admin through the proxy)",
                 self.cfg.admin.port, self.cfg.proxy.admin_host)

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def reconfigure(self, cfg: Config) -> None:
        self.cfg = cfg
        self.pin.cfg = cfg.admin
        if cfg.admin.attempts_log != (str(self.attempts.path) if self.attempts.path else None):
            self.attempts = AttemptLog(cfg.admin.attempts_log)

    def update(self, sets: dict[str, Any], resets: Iterable[str], allow_hidden: bool = False) -> Config:
        """Validate, apply to the running proxy, then save. If saving fails
        the running proxy goes back to the old config."""
        with self._apply_lock:
            doc, cfg = self.file.prepare(sets, resets, allow_hidden)
            old = self.app.cfg
            self._apply(cfg)
            try:
                self.file.write(doc)
            except AdminError:
                self._apply(old)
                raise
            return cfg

    def reload(self) -> Config:
        with self._apply_lock:
            try:
                cfg = config.from_override(self.file.override())
            except ConfigError as e:
                raise AdminError(400, str(e)) from e
            self._apply(cfg)
            return cfg

    def _apply(self, cfg: Config) -> None:
        try:
            self.app.apply_config(cfg)
        except AdminError:
            raise
        except Exception as e:
            log.warning("admin: applying config failed: %s", e)
            raise AdminError(400, f"Couldn't apply: {type(e).__name__}: {e}") from e
        self.reconfigure(cfg)

    def config_view(self) -> dict:
        defaults = config.defaults()
        try:
            override = self.file.override()
            error = ""
        except ConfigError as e:
            override, error = {}, str(e)
        current = config.merged(override) if not error else defaults
        help_ = defaults_help()
        sections = []
        for section, values in defaults.items():
            keys = []
            for key, default in values.items():
                dotted = f"{section}.{key}"
                if dotted in HIDDEN_KEYS or dotted in NOT_SHOWN_KEYS:
                    continue
                value = current[section][key]
                keys.append({
                    "key": key, "value": value, "default": default,
                    "overridden": isinstance(override.get(section), dict) and key in override[section],
                    "help": help_.get(section, {}).get(key, ""),
                    "nullable": default is None or dotted in NULLABLE_KEYS,
                    "restart": dotted in RESTART_KEYS,
                    "pending_restart": dotted in RESTART_KEYS
                                       and value != self.boot.get(section, {}).get(key),
                })
            sections.append({"name": section, "keys": keys})
        detection = current["detection"]
        presets = detection["presets"] or {}
        levels = current["model"]["resource_levels"]
        levels = levels if isinstance(levels, dict) else {}
        models = []
        for m in current["model"]["available"] or ():
            path = m.get("path") or None
            models.append({"name": m.get("name"), "label": m.get("label") or m.get("name"),
                           "description": m.get("description") or "",
                           "path": path, "resolution": m.get("resolution"),
                           "installed": path is None or Path(path).is_file()})
        return {"path": str(self.file.path) if self.file.path else None,
                "editable": self.file.editable, "error": error, "sections": sections,
                "preset": {"active": detection["preset"],
                           "options": [{"name": n, "label": (p or {}).get("label") or "",
                                        "description": (p or {}).get("description") or "",
                                        "censors": preset_censors(p, defaults["detection"])}
                                       for n, p in presets.items()]},
                "models": models,
                "resources": {"active": str(current["model"]["resources"]),
                              "workers": current["model"]["workers"],
                              "threads_per_worker": current["model"]["threads_per_worker"],
                              "video_jobs": current["video"]["max_concurrent_jobs"],
                              "options": [{"name": n, "label": (l or {}).get("label") or "",
                                           "description": (l or {}).get("description") or "",
                                           **{f: (l or {}).get(f) for f in RESOURCE_KEYS.values()}}
                                          for n, l in levels.items()]}}


class _Handler(BaseHTTPRequestHandler):
    admin: AdminServer
    protocol_version = "HTTP/1.1"
    server_version = "scs-admin"
    sys_version = ""

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    # -- request checks ---------------------------------------------------

    def _host(self) -> tuple[str, bool]:
        """(origin, https) for the Host this request came in on."""
        host = (self.headers.get("Host") or "").strip().lower()
        name = host[:host.find("]") + 1] if host.startswith("[") else host.split(":", 1)[0]
        cfg = self.admin.cfg
        if name == cfg.proxy.admin_host:
            return f"https://{name}", True
        if name in cfg.admin.allowed_hosts:
            return f"http://{host}", False
        raise AdminError(421, "unknown host")

    def _check_api(self, method: str, origin: str) -> None:
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            raise AdminError(403, "cross-site request refused")
        if method != "GET":
            if self.headers.get("Origin") != origin:
                raise AdminError(403, "origin not allowed")
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                raise AdminError(415, "expected application/json")

    def _token(self) -> str | None:
        try:
            morsel = SimpleCookie(self.headers.get("Cookie") or "").get(COOKIE)
        except CookieError:
            return None
        return morsel.value if morsel else None

    def _read_body(self) -> None:
        """Read the body before anything can refuse the request, so a refused
        request's body isn't left on the connection as the next request."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            self.close_connection = True
            self._raw = b""
            raise AdminError(413, "body too large")
        self._raw = self.rfile.read(length) if length else b""

    def _body(self) -> dict:
        try:
            body = json.loads(self._raw or b"{}")
        except ValueError as e:
            raise AdminError(400, "body is not JSON") from e
        if not isinstance(body, dict):
            raise AdminError(400, "body must be a JSON object")
        return body

    # -- responses --------------------------------------------------------

    def _send(self, status: int, data: bytes, ctype: str, headers: Iterable[tuple[str, str]] = ()) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
                         "base-uri 'none'; form-action 'none'")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, status: int, body: Any, headers: Iterable[tuple[str, str]] = ()) -> None:
        self._send(status, json.dumps(body).encode(), "application/json", headers)

    def _cookie(self, token: str | None, https: bool) -> tuple[str, str]:
        attrs = "Path=/admin; HttpOnly; SameSite=Strict" + ("; Secure" if https else "")
        if token is None:
            return "Set-Cookie", f"{COOKIE}=; Max-Age=0; {attrs}"
        return "Set-Cookie", f"{COOKIE}={token}; {attrs}"

    # -- routing ----------------------------------------------------------

    def do_GET(self):
        self._handle("GET")

    def do_HEAD(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def _handle(self, method: str) -> None:
        try:
            self._read_body()
            origin, https = self._host()
            self._via = origin.split("://", 1)[1]
            path = urlsplit(self.path).path
            if method == "GET" and path in ("/", "/admin"):
                return self._send(HTTPStatus.FOUND, b"", "text/plain", [("Location", "/admin/")])
            if method == "GET" and path == "/admin/":
                return self._send(200, (STATIC / "admin.html").read_bytes(), "text/html; charset=utf-8")
            if method == "GET" and path.startswith("/admin/") and path[7:] in STATIC_FILES:
                return self._send(200, (STATIC / path[7:]).read_bytes(), STATIC_FILES[path[7:]])
            if not path.startswith("/admin/api/"):
                raise AdminError(404, "not found")
            self._check_api(method, origin)
            self._api(method, path[len("/admin/api/"):], https)
        except AdminError as e:
            self._json(e.status, e.body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # pragma: no cover - logged, never leaks a trace
            log.exception("admin: %s %s failed", method, self.path)
            self._json(500, {"error": f"{type(e).__name__}: {e}"})

    def _api(self, method: str, name: str, https: bool) -> None:
        admin, pin = self.admin, self.admin.pin
        token = self._token()
        if (method, name) == ("GET", "session"):
            return self._json(200, {"pin_set": pin.pin_set, "unlocked": pin.valid(token, touch=False)})
        if (method, name) == ("POST", "unlock"):
            if not pin.pin_set:
                return self._json(200, {"unlocked": True})
            self._check_pin(self._body().get("pin", ""), "unlock")
            pin.close(token)
            return self._json(200, {"unlocked": True}, [self._cookie(pin.open_session(), https)])

        if not pin.valid(token, touch=self.headers.get("X-Admin-Background") != "1"):
            raise AdminError(401, "The panel is locked.", locked=True)

        if (method, name) == ("POST", "lock"):
            pin.close(token)
            return self._json(200, {"unlocked": not pin.pin_set}, [self._cookie(None, https)])
        if (method, name) == ("POST", "pin"):
            return self._set_pin(self._body(), https)
        if (method, name) == ("GET", "attempts"):
            return self._json(200, {"attempts": admin.attempts.unacknowledged()})
        if (method, name) == ("POST", "attempts/ack"):
            admin.attempts.acknowledge()
            return self._json(200, {"attempts": []})
        if (method, name) == ("GET", "status"):
            return self._json(200, {**admin.app.status(), "pin_set": pin.pin_set,
                                    "wrong_pin_entries": len(admin.attempts.unacknowledged()),
                                    "config_path": str(admin.file.path) if admin.file.path else None,
                                    "config_editable": admin.file.editable,
                                    "version": updates.version(),
                                    "updates": {"enabled": admin.cfg.updates.enabled,
                                                "wait_for_browser": admin.cfg.updates.wait_for_browser,
                                                "last": updates.read_status(admin.update_status)}})
        if (method, name) == ("GET", "log"):
            query = parse_qs(urlsplit(self.path).query)
            try:
                limit = int(query.get("limit", [admin.cfg.admin.log_entries])[0])
            except ValueError:
                limit = admin.cfg.admin.log_entries
            lcfg = admin.cfg.logging
            entries = read_log_tail(lcfg.path, max(1, min(limit, 5000))) if lcfg.level != "off" else []
            return self._json(200, {"level": lcfg.level, "path": lcfg.path, "entries": entries})
        if (method, name) == ("GET", "jobs"):
            jobs = admin.app.jobs
            ready = admin.cfg.video.ready_seconds
            return self._json(200, {"jobs": [job_view(j, ready) for j in jobs.list()] if jobs else []})
        if method == "POST" and (m := CANCEL_PATH.match(name)):
            jobs = admin.app.jobs
            if not jobs or not jobs.get(m.group(1)):
                raise AdminError(404, "no such job")
            jobs.cancel(m.group(1), "cancelled from the admin panel")
            return self._json(200, {"ok": True})
        if (method, name) == ("GET", "config"):
            return self._json(200, admin.config_view())
        if (method, name) == ("POST", "config"):
            body = self._body()
            sets, resets = body.get("set") or {}, body.get("reset") or []
            if not isinstance(sets, dict) or not isinstance(resets, list):
                raise AdminError(400, "set must be an object and reset a list")
            admin.update(sets, resets)
            log.info("admin: settings changed: %s", ", ".join([*sets, *resets]) or "nothing")
            return self._json(200, admin.config_view())
        if (method, name) == ("POST", "config/reload"):
            admin.reload()
            log.info("admin: config reloaded from file")
            return self._json(200, admin.config_view())
        raise AdminError(404, "not found")

    def _check_pin(self, pin: Any, action: str) -> None:
        """Raise 403 on a wrong PIN, after recording it for the report."""
        if not self.admin.pin.check(str(pin)):
            self.admin.attempts.record(action, self._via, self.headers.get("User-Agent"))
            raise AdminError(403, "Wrong PIN.")

    def _set_pin(self, body: dict, https: bool) -> None:
        admin, pin = self.admin, self.admin.pin
        new = body.get("new") or None
        if pin.pin_set:
            self._check_pin(body.get("current") or "", "change PIN" if new else "remove PIN")
        if new is not None and not PIN.match(str(new)):
            raise AdminError(400, "A PIN is 4 to 12 digits.")
        if new is None and not pin.pin_set:
            return self._json(200, {"pin_set": False, "unlocked": True})
        if new is None:
            admin.update({}, ["admin.pin_hash"], allow_hidden=True)
        else:
            admin.update({"admin.pin_hash": hash_pin(str(new))}, [], allow_hidden=True)
        pin.close_all()
        log.info("admin: PIN %s", "set" if new else "removed")
        headers = [self._cookie(pin.open_session(), https)] if new else [self._cookie(None, https)]
        return self._json(200, {"pin_set": new is not None, "unlocked": True}, headers)
