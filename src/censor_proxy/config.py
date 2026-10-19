"""Configuration: packaged defaults deep-merged with an optional user YAML file."""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULTS_PATH = Path(__file__).with_name("defaults.yaml")
CONFIG_ENV = "CENSOR_CONFIG"

CENSOR_STYLES = ("gaussian", "pixelate", "solid")
CUSTOM_PRESET = "custom"
# Detection keys a preset can't set.
PRESET_KEYS = frozenset({"preset", "presets"})
LOG_LEVELS = ("off", "info", "debug")
DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?\.)*"
                    r"[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ModelChoice:
    name: str
    label: str
    description: str
    path: str | None
    resolution: int


@dataclass(frozen=True)
class ModelConfig:
    path: str | None
    resolution: int
    workers: int
    threads_per_worker: int
    tiling: bool
    available: tuple[ModelChoice, ...]

    def name_for(self, path: str | None, resolution: int) -> str:
        """The label and name of the listed model with these weights, else the path."""
        for m in self.available:
            if m.path == path and (path is None or m.resolution == resolution):
                return m.name if m.label == m.name else f"{m.label} ({m.name})"
        return path or "320n (bundled)"


@dataclass(frozen=True)
class DetectionConfig:
    preset: str
    threshold: float
    label_thresholds: dict[str, float]
    corroborated_threshold: float
    male_context_threshold: float
    female_labels: frozenset[str]
    unsexed_labels: frozenset[str]
    female_context_labels: frozenset[str]
    male_context_labels: frozenset[str]
    context_threshold: float
    overlap_context_threshold: float
    proximity: float
    male_context_exempts: bool
    male_counterparts: dict[str, str]
    male_score_ratio: float
    yields_to_male: frozenset[str]
    male_priority_labels: frozenset[str]
    male_priority_threshold: float
    male_priority_overlap: float

    def threshold_for(self, label: str) -> float:
        return self.label_thresholds.get(label, self.threshold)


@dataclass(frozen=True)
class CensorConfig:
    style: str
    padding: float
    blur_strength: float
    pixel_blocks: int
    fill_color: tuple[int, int, int]
    fail_closed: bool
    timeout_seconds: float
    quality: int
    avif_speed: int


@dataclass(frozen=True)
class ImagesConfig:
    size_floor_bytes: int
    min_side_px: int
    strip_accept: frozenset[str]
    sniff_image_requests: bool
    sniff_types: frozenset[str]
    sniff_max_bytes: int
    max_bytes: int
    max_decoded_bytes: int
    frame_step: int
    max_detect_frames: int
    max_frames: int
    placeholders: bool
    placeholder_delay_ms: int
    placeholder_min_px: int
    placeholder_reserve_px: int


@dataclass(frozen=True)
class DataUrisConfig:
    enabled: bool
    content_types: frozenset[str]
    max_body_bytes: int
    svg_depth: int

    def matches(self, mime: str) -> bool:
        """Whether a Content-Type (no parameters) is scanned. Entries may be
        globs, e.g. application/*+json."""
        return mime in self.content_types or any(
            fnmatch.fnmatchcase(mime, t) for t in self.content_types if "*" in t)


@dataclass(frozen=True)
class VideoConfig:
    enabled: bool
    hosts: tuple[str, ...]
    model_path: str | None
    model_resolution: int
    tiling: bool
    sample_fps: float
    max_height: int
    max_fps: float
    padding: float
    audio: bool
    preset: str
    crf: int
    ready_seconds: float
    segment_seconds: float
    seek_wait_seconds: float
    max_duration_seconds: float
    max_concurrent_jobs: int
    max_queued_jobs: int
    max_starts_per_minute: int
    source_protocols: tuple[str, ...]
    abandon_seconds: float
    idle_seconds: float
    allow_private_sources: bool
    cache_dir: str
    cache_max_bytes: int
    brand_name: str
    debug: bool


@dataclass(frozen=True)
class VideoAttributionConfig:
    enabled: bool
    model_path: str | None
    model_resolution: int
    tiling: bool
    sample_fps: float
    history_seconds: float
    lookahead_seconds: float
    male_labels: frozenset[str]
    male_threshold: float
    min_male_samples: int
    female_labels: frozenset[str]
    female_threshold: float
    max_female_samples: int
    pass_labels: frozenset[str]
    stall_seconds: float


@dataclass(frozen=True)
class ProxyConfig:
    health_host: str
    admin_host: str
    internal_port: int


@dataclass(frozen=True)
class WhitelistConfig:
    domains: tuple[str, ...]
    builtin: tuple[str, ...]
    match_referer: bool

    @property
    def patterns(self) -> tuple[str, ...]:
        """Every whitelisted host as a regex on host:port (mitmproxy's
        ignore_hosts): the built-in list, then the domains."""
        return self.builtin + tuple(rf"(^|\.){re.escape(d)}:" for d in self.domains)

    def match(self, host: str, port: int = 443) -> str | None:
        """The whitelisted domain covering `host` (itself or a parent), or
        the built-in pattern matching it, if any."""
        host = host.lower().rstrip(".")
        for domain in self.domains:
            if host == domain or host.endswith("." + domain):
                return domain
        address = f"{host}:{port}"
        return next((p for p in self.builtin if re.search(p, address, re.IGNORECASE)), None)


@dataclass(frozen=True)
class AdminConfig:
    enabled: bool
    bind: str
    port: int
    allowed_hosts: frozenset[str]
    pin_hash: str | None
    attempts_log: str | None
    session_seconds: float
    log_entries: int


@dataclass(frozen=True)
class CacheConfig:
    max_entries: int


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    path: str | None
    max_bytes: int
    backups: int


@dataclass(frozen=True)
class Config:
    model: ModelConfig
    detection: DetectionConfig
    censor: CensorConfig
    images: ImagesConfig
    data_uris: DataUrisConfig
    video: VideoConfig
    video_attribution: VideoAttributionConfig
    proxy: ProxyConfig
    whitelist: WhitelistConfig
    admin: AdminConfig
    cache: CacheConfig
    logging: LoggingConfig


def normalize_domain(value: str) -> str:
    """youtube.com from youtube.com, *.YouTube.com., www.youtube.com, or a URL.
    A leading www. is dropped so the entry covers the whole site."""
    value = str(value).strip().lower()
    if "://" in value:
        value = value.split("://", 1)[1]
    value = value.split("/", 1)[0].split("?", 1)[0].rsplit("@", 1)[-1]
    if not value.startswith("["):
        value = value.split(":", 1)[0]
    return value.removeprefix("*.").strip(".").removeprefix("www.")


def _merge(defaults: dict, override: dict) -> dict:
    """Merge section by section. Values inside a section (including lists and
    label_thresholds) replace the default wholesale."""
    out = {}
    for section, values in override.items():
        if section not in defaults:
            raise ConfigError(f"unknown config section: {section}")
        if not isinstance(values, dict):
            raise ConfigError(f"config section {section} must be a mapping")
        for key in values:
            if key not in defaults[section]:
                raise ConfigError(f"unknown config key: {section}.{key}")
        out[section] = {**defaults[section], **values}
    return {**defaults, **out}


def _apply_preset(raw: dict, defaults_: dict) -> dict:
    """Replace the detection section with the defaults plus the chosen
    preset's settings, unless the preset is custom."""
    d = raw["detection"]
    presets = d["presets"] or {}
    if not isinstance(presets, dict):
        raise ConfigError("detection.presets must be a mapping")
    for name, preset in presets.items():
        if name == CUSTOM_PRESET:
            raise ConfigError("detection.presets: 'custom' is reserved")
        if not isinstance(preset, dict) or not isinstance(preset.get("settings") or {}, dict):
            raise ConfigError(f"detection.presets.{name} needs a settings mapping")
        for key in preset.get("settings") or {}:
            if key not in defaults_["detection"] or key in PRESET_KEYS:
                raise ConfigError(f"detection.presets.{name}: unknown detection key {key}")
    name = d["preset"]
    if name == CUSTOM_PRESET:
        return raw
    if name not in presets:
        raise ConfigError(f"detection.preset must be {CUSTOM_PRESET} or one of "
                          f"{sorted(presets)}, got {name!r}")
    settings = presets[name].get("settings") or {}
    return {**raw, "detection": {**defaults_["detection"], **settings,
                                 "preset": name, "presets": presets}}


def _build(raw: dict[str, Any]) -> Config:
    d = raw["detection"]
    c = raw["censor"]
    m = raw["model"]
    available = []
    for entry in m["available"] or ():
        try:
            available.append(ModelChoice(name=str(entry["name"]),
                                         label=str(entry.get("label") or entry["name"]),
                                         description=str(entry.get("description") or ""),
                                         path=entry.get("path") or None,
                                         resolution=int(entry["resolution"])))
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise ConfigError(f"model.available: each entry needs name and resolution ({e})") from e
    sections = {
        "model": ModelConfig(**{**m, "available": tuple(available)}),
        "detection": DetectionConfig(
            preset=str(d["preset"]),
            threshold=float(d["threshold"]),
            label_thresholds={str(k): float(v) for k, v in (d["label_thresholds"] or {}).items()},
            corroborated_threshold=float(d["corroborated_threshold"]),
            male_context_threshold=float(d["male_context_threshold"]),
            female_labels=frozenset(d["female_labels"]),
            unsexed_labels=frozenset(d["unsexed_labels"]),
            female_context_labels=frozenset(d["female_context_labels"]),
            male_context_labels=frozenset(d["male_context_labels"]),
            context_threshold=float(d["context_threshold"]),
            overlap_context_threshold=float(d["overlap_context_threshold"]),
            proximity=float(d["proximity"]),
            male_context_exempts=bool(d["male_context_exempts"]),
            male_counterparts={str(k): str(v) for k, v in (d["male_counterparts"] or {}).items()},
            male_score_ratio=float(d["male_score_ratio"]),
            yields_to_male=frozenset(d["yields_to_male"] or ()),
            male_priority_labels=frozenset(d["male_priority_labels"] or ()),
            male_priority_threshold=float(d["male_priority_threshold"]),
            male_priority_overlap=float(d["male_priority_overlap"]),
        ),
        "censor": CensorConfig(**{**c, "fill_color": tuple(c["fill_color"])}),
        "images": ImagesConfig(**{
            **raw["images"],
            "strip_accept": frozenset(t.lower() for t in raw["images"]["strip_accept"] or ()),
            "sniff_types": frozenset((t or "").lower() for t in raw["images"]["sniff_types"] or ()),
        }),
        "data_uris": DataUrisConfig(
            enabled=bool(raw["data_uris"]["enabled"]),
            content_types=frozenset(t.lower() for t in raw["data_uris"]["content_types"] or ()),
            max_body_bytes=int(raw["data_uris"]["max_body_bytes"]),
            svg_depth=int(raw["data_uris"]["svg_depth"]),
        ),
        "video": VideoConfig(**{**raw["video"], "hosts": tuple(raw["video"]["hosts"] or ()),
                                "source_protocols": tuple(raw["video"]["source_protocols"] or ())}),
        "video_attribution": VideoAttributionConfig(**{
            **raw["video_attribution"],
            **{k: frozenset(raw["video_attribution"][k] or ())
               for k in ("male_labels", "female_labels", "pass_labels")},
        }),
        "proxy": ProxyConfig(
            health_host=raw["proxy"]["health_host"],
            admin_host=str(raw["proxy"]["admin_host"]).lower(),
            internal_port=int(raw["proxy"]["internal_port"]),
        ),
        "whitelist": WhitelistConfig(
            domains=tuple(dict.fromkeys(normalize_domain(d)
                                        for d in raw["whitelist"]["domains"] or ())),
            builtin=tuple(raw["whitelist"]["builtin"] or ()),
            match_referer=bool(raw["whitelist"]["match_referer"]),
        ),
        "admin": AdminConfig(**{
            **raw["admin"],
            "allowed_hosts": frozenset(str(h).lower() for h in raw["admin"]["allowed_hosts"] or ()),
            "pin_hash": raw["admin"]["pin_hash"] or None,
            "attempts_log": raw["admin"]["attempts_log"] or None,
        }),
        "cache": CacheConfig(**raw["cache"]),
        "logging": LoggingConfig(**raw["logging"]),
    }
    cfg = Config(**sections)
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    if cfg.censor.style not in CENSOR_STYLES:
        raise ConfigError(f"censor.style must be one of {CENSOR_STYLES}, got {cfg.censor.style!r}")
    if cfg.logging.level not in LOG_LEVELS:
        raise ConfigError(f"logging.level must be one of {LOG_LEVELS}, got {cfg.logging.level!r}")
    if cfg.detection.male_score_ratio < 0:
        raise ConfigError("detection.male_score_ratio must be >= 0")
    if not 0 < cfg.detection.male_priority_overlap <= 1:
        raise ConfigError("detection.male_priority_overlap must be in (0, 1]")
    if cfg.images.frame_step < 1:
        raise ConfigError("images.frame_step must be >= 1")
    if cfg.images.max_detect_frames < 2:
        raise ConfigError("images.max_detect_frames must be >= 2")
    if min(cfg.images.placeholder_delay_ms, cfg.images.placeholder_min_px,
           cfg.images.placeholder_reserve_px) < 0:
        raise ConfigError("images.placeholder_delay_ms, placeholder_min_px and "
                          "placeholder_reserve_px must be >= 0")
    if not 1 <= cfg.censor.quality <= 100:
        raise ConfigError("censor.quality must be between 1 and 100")
    if len(cfg.censor.fill_color) != 3:
        raise ConfigError("censor.fill_color must be [R, G, B]")
    for key, patterns in (("whitelist.builtin", cfg.whitelist.builtin),
                          ("video.hosts", cfg.video.hosts)):
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as e:
                raise ConfigError(f"{key}: bad regex {pattern!r}: {e}") from e
    if cfg.video.sample_fps <= 0:
        raise ConfigError("video.sample_fps must be > 0")
    if cfg.video.segment_seconds <= 0 or cfg.video.seek_wait_seconds < 0:
        raise ConfigError("video.segment_seconds must be > 0 and video.seek_wait_seconds >= 0")
    if cfg.video.abandon_seconds <= 0 or cfg.video.idle_seconds <= 0:
        raise ConfigError("video.abandon_seconds and video.idle_seconds must be > 0")
    a = cfg.video_attribution
    if a.sample_fps <= 0 or a.stall_seconds <= 0:
        raise ConfigError("video_attribution.sample_fps and stall_seconds must be > 0")
    if a.history_seconds < 0 or a.lookahead_seconds < 0:
        raise ConfigError("video_attribution.history_seconds and lookahead_seconds must be >= 0")
    if a.min_male_samples < 1 or a.max_female_samples < 0:
        raise ConfigError("video_attribution.min_male_samples must be >= 1 and "
                          "max_female_samples >= 0")
    for domain in cfg.whitelist.domains:
        if not DOMAIN.match(domain):
            raise ConfigError(f"whitelist.domains: not a domain: {domain!r}")
    if not 0 < cfg.admin.port < 65536:
        raise ConfigError("admin.port must be between 1 and 65535")
    if cfg.admin.port == cfg.proxy.internal_port:
        raise ConfigError("admin.port and proxy.internal_port must differ")
    if cfg.admin.session_seconds <= 0:
        raise ConfigError("admin.session_seconds must be > 0")


def defaults() -> dict:
    return yaml.safe_load(DEFAULTS_PATH.read_text())


def merged(override: dict, apply_preset: bool = True) -> dict:
    """The effective raw config: defaults with a user override merged in, and
    the detection preset applied (unless apply_preset is false)."""
    defaults_ = defaults()
    raw = _merge(defaults_, override)
    # Presets merge by name, so config.yaml can add any number without
    # repeating the built-in ones; a preset set to null is removed.
    user_presets = (override.get("detection") or {}).get("presets")
    if isinstance(user_presets, dict):
        presets = {**(defaults_["detection"]["presets"] or {}), **user_presets}
        raw["detection"] = {**raw["detection"],
                            "presets": {k: v for k, v in presets.items() if v is not None}}
    return _apply_preset(raw, defaults_) if apply_preset else raw


def from_override(override: dict) -> Config:
    return _build(merged(override))


def config_path(path: str | os.PathLike | None = None) -> Path | None:
    """The user config file: `path`, else $CENSOR_CONFIG. None if neither is set."""
    path = path if path is not None else os.environ.get(CONFIG_ENV)
    return Path(path) if path else None


def read_override(path: Path | None) -> dict:
    if not path or not path.is_file():
        return {}
    override = yaml.safe_load(path.read_text()) or {}
    if not isinstance(override, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return override


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Load defaults, then merge the user file at `path` (or $CENSOR_CONFIG) if it exists."""
    return from_override(read_override(config_path(path)))
