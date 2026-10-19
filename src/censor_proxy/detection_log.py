"""JSONL detection log.

Levels:
  off    nothing is written
  info   images with an explicit-region detection (censored or not), and
         every fail-closed or error; hostname only
  debug  every image response, including skipped ones, with the full URL
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import time

from .config import Config
from .imaging import ScanResult

_logger = logging.getLogger("censor_proxy.detections")
_logger.propagate = False


class DetectionLog:
    def __init__(self, cfg: Config):
        self.level = cfg.logging.level
        dcfg = cfg.detection
        self._relevant = (dcfg.female_labels | dcfg.unsexed_labels
                          | {label for label in dcfg.male_context_labels if not label.startswith("FACE_")})
        for h in list(_logger.handlers):
            _logger.removeHandler(h)
            h.close()
        if self.level != "off" and cfg.logging.path:
            os.makedirs(os.path.dirname(cfg.logging.path) or ".", exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                cfg.logging.path, maxBytes=cfg.logging.max_bytes, backupCount=cfg.logging.backups)
            handler.setFormatter(logging.Formatter("%(message)s"))
            _logger.addHandler(handler)
            _logger.setLevel(logging.INFO)

    def record(self, result: ScanResult, host: str, url: str, mime: str,
               source: str = "response") -> None:
        if self.level == "off" or not _logger.handlers:
            return
        # Images that couldn't be decoded passed through uncensored: worth
        # seeing without debug logging (icons and SVG aside).
        undecodable = (result.action == "skipped"
                       and result.reason.startswith(("not a decodable image", "unsupported format"))
                       and mime not in ("image/svg+xml", "image/x-icon", "image/vnd.microsoft.icon")
                       and result.format != "ICO")
        notable = undecodable or result.action in ("censored", "fail_closed", "error") or any(
            d.detection.label in self._relevant for _, ds in result.frame_decisions for d in ds)
        if not notable and self.level != "debug":
            return
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "host": host,
            **({"url": url} if self.level == "debug" else {}),
            "mime": mime,
            **({"source": source} if source != "response" else {}),
            "format": result.format,
            "action": result.action,
            **({"reason": result.reason} if result.reason else {}),
            "frames": result.frames,
            "elapsed_ms": round(result.elapsed_ms, 1),
            "cached": result.cached,
            "detections": [
                {**d.detection.as_dict(), "censor": d.censor, "why": d.reason,
                 **({"frame": i} if result.frames > 1 else {})}
                for i, ds in result.frame_decisions for d in ds
            ],
        }
        _logger.info(json.dumps(entry, separators=(",", ":")))
