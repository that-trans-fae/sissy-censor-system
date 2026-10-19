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
LOG_LEVELS = ("off", "info", "debug")
DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?\.)*"
                    r"[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ModelConfig:
    path: str | None
    resolution: int
    workers: int
    threads_per_worker: int
    tiling: bool


@dataclass(frozen=True)
class DetectionConfig:
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
    max_duration_seconds: float
    max_concurrent_jobs: int
    abandon_seconds: float
    idle_seconds: float
    allow_private_sources: bool
    cache_dir: str
    cache_max_bytes: int
    brand_name: str
    debug: bool


@dataclass(frozen=True)
class ProxyConfig:
    passthrough_hosts: tuple[str, ...]
    health_host: str
    admin_host: str
    internal_port: int


@dataclass(frozen=True)
class WhitelistConfig:
    domains: tuple[str, ...]
    match_referer: bool

    def match(self, host: str) -> str | None:
        """The whitelisted domain covering `host` (itself or a parent), if any."""
        host = host.lower().rstrip(".")
        for domain in self.domains:
            if host == domain or host.endswith("." + domain):
                return domain
        return None


@dataclass(frozen=True)
class AdminConfig:
    enabled: bool
    bind: str
    port: int
    allowed_hosts: frozenset[str]
    pin_hash: str | None
    pin_max_attempts: int
    pin_lockout_seconds: float
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


def _build(raw: dict[str, Any]) -> Config:
    d = raw["detection"]
    c = raw["censor"]
    sections = {
        "model": ModelConfig(**raw["model"]),
        "detection": DetectionConfig(
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
        "video": VideoConfig(**{**raw["video"], "hosts": tuple(raw["video"]["hosts"] or ())}),
        "proxy": ProxyConfig(
            passthrough_hosts=tuple(raw["proxy"]["passthrough_hosts"] or ()),
            health_host=raw["proxy"]["health_host"],
            admin_host=str(raw["proxy"]["admin_host"]).lower(),
            internal_port=int(raw["proxy"]["internal_port"]),
        ),
        "whitelist": WhitelistConfig(
            domains=tuple(dict.fromkeys(normalize_domain(d)
                                        for d in raw["whitelist"]["domains"] or ())),
            match_referer=bool(raw["whitelist"]["match_referer"]),
        ),
        "admin": AdminConfig(**{
            **raw["admin"],
            "allowed_hosts": frozenset(str(h).lower() for h in raw["admin"]["allowed_hosts"] or ()),
            "pin_hash": raw["admin"]["pin_hash"] or None,
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
    if cfg.images.frame_step < 1:
        raise ConfigError("images.frame_step must be >= 1")
    if cfg.images.max_detect_frames < 2:
        raise ConfigError("images.max_detect_frames must be >= 2")
    if not 1 <= cfg.censor.quality <= 100:
        raise ConfigError("censor.quality must be between 1 and 100")
    if len(cfg.censor.fill_color) != 3:
        raise ConfigError("censor.fill_color must be [R, G, B]")
    for key, patterns in (("proxy.passthrough_hosts", cfg.proxy.passthrough_hosts),
                          ("video.hosts", cfg.video.hosts)):
        for pattern in patterns:
            try:
                re.compile(pattern)
            except re.error as e:
                raise ConfigError(f"{key}: bad regex {pattern!r}: {e}") from e
    if cfg.video.sample_fps <= 0:
        raise ConfigError("video.sample_fps must be > 0")
    if cfg.video.abandon_seconds <= 0 or cfg.video.idle_seconds <= 0:
        raise ConfigError("video.abandon_seconds and video.idle_seconds must be > 0")
    for domain in cfg.whitelist.domains:
        if not DOMAIN.match(domain):
            raise ConfigError(f"whitelist.domains: not a domain: {domain!r}")
    if not 0 < cfg.admin.port < 65536:
        raise ConfigError("admin.port must be between 1 and 65535")
    if cfg.admin.port == cfg.proxy.internal_port:
        raise ConfigError("admin.port and proxy.internal_port must differ")
    if cfg.admin.pin_max_attempts < 1 or cfg.admin.session_seconds <= 0:
        raise ConfigError("admin.pin_max_attempts must be >= 1 and admin.session_seconds > 0")


def defaults() -> dict:
    return yaml.safe_load(DEFAULTS_PATH.read_text())


def merged(override: dict) -> dict:
    """The effective raw config: defaults with a user override merged in."""
    return _merge(defaults(), override)


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
