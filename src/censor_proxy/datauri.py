"""Censor base64 data-URI images embedded in text bodies (HTML, CSS, JSON).

A single regex over the raw bytes covers <img src>, srcset, inline styles,
CSS url() and JSON strings alike, so the body is never parsed. JSON may
escape "/" as "\\/", both in "data:image\\/png" and inside the payload; that
is undone for decoding and reapplied when the censored image is written back.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field

from .imaging import Censor, ScanResult

DATA_URI = re.compile(rb"data:image\\?/(png|jpe?g|gif|webp);base64,((?:[A-Za-z0-9+=]|\\?/)+)")


@dataclass
class DataUriResult:
    body: bytes
    # (mime, result) for every data-URI image that decoded
    results: list[tuple[str, ScanResult]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return any(r.changed for _, r in self.results)


def find(body: bytes) -> list[re.Match]:
    if b"data:image" not in body:
        return []
    return list(DATA_URI.finditer(body))


def censor_data_uris(body: bytes, censor: Censor, *, fail_closed: bool,
                     matches: list[re.Match] | None = None,
                     force_reason: str | None = None) -> DataUriResult:
    """Run every data-URI image in `body` through censor_bytes and splice the
    results back in.

    A failure on one image fails that image closed (or leaves it, if
    fail_closed is off) without affecting the rest. With `force_reason`, every
    image is failed closed without detection; the caller uses this when the
    whole body timed out.
    """
    if matches is None:
        matches = find(body)
    out, results, last = [], [], 0
    for m in matches:
        payload = m.group(2)
        escaped = b"\\/" in payload
        try:
            raw = payload.replace(b"\\/", b"/")
            data = base64.b64decode(raw + b"=" * (-len(raw) % 4), validate=True)
        except (binascii.Error, ValueError):
            continue
        mime = "image/" + m.group(1).decode()
        if force_reason:
            result = censor.fail_closed(data, force_reason)
        else:
            try:
                result = censor.censor_bytes(data, mime)
            except Exception as e:
                reason = f"error: {type(e).__name__}: {e}"
                result = (censor.fail_closed(data, reason) if fail_closed
                          else ScanResult(data, "error", f"{reason}; fail_closed off, delivered unmodified"))
        results.append((mime, result))
        if result.changed:
            encoded = base64.b64encode(result.data)
            if escaped:
                encoded = encoded.replace(b"/", b"\\/")
            out += [body[last:m.start(2)], encoded]
            last = m.end(2)
    if last == 0:
        return DataUriResult(body, results)
    out.append(body[last:])
    return DataUriResult(b"".join(out), results)
