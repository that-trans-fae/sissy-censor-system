"""Censor base64 data-URI images embedded in text bodies (HTML, CSS, JSON).

A single regex over the raw bytes covers <img src>, srcset, inline styles,
CSS url() and JSON strings alike, so the body is never parsed. JSON may
escape "/" as "\\/", both in "data:image\\/png" and inside the payload; that
is undone for decoding and reapplied when the censored image is written back.
Parameters before ";base64" (";charset=utf-8") and any letter case are
accepted. Base64 SVGs are decoded and scanned the same way for images inside
them, up to data_uris.svg_depth deep.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field

from .imaging import Censor, ScanResult

DATA_URI = re.compile(
    rb"data:image\\?/(png|jpe?g|gif|webp|avif|svg\+xml)(?:;[^;,\s\"'<>()]*)*?;base64,"
    rb"((?:[A-Za-z0-9+=]|\\?/)+)", re.IGNORECASE)
SVG = "image/svg+xml"


@dataclass
class DataUriResult:
    body: bytes
    # (mime, result) for every data-URI image that decoded
    results: list[tuple[str, ScanResult]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return any(r.changed for _, r in self.results)


def find(body: bytes) -> list[re.Match]:
    return list(DATA_URI.finditer(body))


def censor_data_uris(body: bytes, censor: Censor, *, fail_closed: bool,
                     matches: list[re.Match] | None = None,
                     force_reason: str | None = None, precheck: bool = False,
                     deadline: float | None = None, _depth: int = 0) -> DataUriResult | None:
    """Run every data-URI image in `body` through censor_bytes and splice the
    results back in.

    A failure on one image fails that image closed (or leaves it, if
    fail_closed is off) without affecting the rest. With `force_reason`, every
    image is failed closed without detection; the caller uses this when the
    whole body timed out. With `precheck`, only Censor.precheck is used, and
    the result is None as soon as one image needs the full scan; the caller
    uses this to skip the detection queue for bodies whose images are all
    tiny placeholders or cached. Past `deadline` (see censor_bytes) the
    remaining images fail closed without detection.
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
        mime = "image/" + m.group(1).decode().lower()
        if mime == SVG:
            if _depth >= censor.cfg.data_uris.svg_depth:
                continue
            inner = censor_data_uris(data, censor, fail_closed=fail_closed,
                                     force_reason=force_reason, precheck=precheck,
                                     deadline=deadline, _depth=_depth + 1)
            if inner is None:
                return None
            results += inner.results
            if inner.changed:
                out += [body[last:m.start(2)], _encode(inner.body, escaped)]
                last = m.end(2)
            continue
        if force_reason:
            result = censor.fail_closed(data, force_reason)
        elif precheck:
            if (result := censor.precheck(data, mime)) is None:
                return None
        else:
            try:
                result = censor.censor_bytes(data, mime, deadline)
            except Exception as e:
                reason = "timeout" if isinstance(e, TimeoutError) else f"error: {type(e).__name__}: {e}"
                result = (censor.fail_closed(data, reason) if fail_closed
                          else ScanResult(data, "error", f"{reason}; fail_closed off, delivered unmodified"))
        results.append((mime, result))
        if result.changed:
            out += [body[last:m.start(2)], _encode(result.data, escaped)]
            last = m.end(2)
    if last == 0:
        return DataUriResult(body, results)
    out.append(body[last:])
    return DataUriResult(b"".join(out), results)


def _encode(data: bytes, escaped: bool) -> bytes:
    encoded = base64.b64encode(data)
    return encoded.replace(b"/", b"\\/") if escaped else encoded
