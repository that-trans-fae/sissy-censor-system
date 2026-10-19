"""Inject the video control script into HTML pages on allowlisted hosts, and
extend the page's Content-Security-Policy just enough to let it run.

CSP is extended, never removed: our origin is added to script-src,
media-src and connect-src (or default-src when those are absent). When the
policy relies on nonces, hashes or 'strict-dynamic', host allowlisting is
ignored by the browser, so the script also gets a fresh nonce. A nonce is
not added otherwise, because its presence would disable a policy's
'unsafe-inline' and break the page's own inline scripts.
"""

from __future__ import annotations

import re
import secrets

HEAD = re.compile(rb"<head\b[^>]*>", re.IGNORECASE)
HTML = re.compile(rb"<html\b[^>]*>", re.IGNORECASE)
DOCTYPE = re.compile(rb"<!doctype[^>]*>", re.IGNORECASE)
MARKER = b"data-censor-proxy"

# directive -> directives it falls back to, in order
_FALLBACKS = {
    "script-src-elem": ("script-src", "default-src"),
    "script-src": ("default-src",),
    "media-src": ("default-src",),
    "connect-src": ("default-src",),
}


def _parse(policy: str) -> list[list[str]]:
    return [d.split() for d in policy.split(";") if d.strip()]


def _needs_nonce(sources: list[str]) -> bool:
    return any(s == "'strict-dynamic'" or s.startswith(("'nonce-", "'sha256-", "'sha384-", "'sha512-"))
               for s in sources)


def extend_policy(policy: str, origin: str, nonce: str) -> tuple[str, bool]:
    """Return (new policy, whether the script must carry `nonce`)."""
    directives = _parse(policy)
    by_name = {d[0].lower(): d for d in directives}
    use_nonce = False
    touched: set[int] = set()
    for name, fallbacks in _FALLBACKS.items():
        if name == "script-src-elem" and name not in by_name:
            continue  # only extended when the page sets it explicitly
        target = by_name.get(name) or next((by_name[f] for f in fallbacks if f in by_name), None)
        if target is None or id(target) in touched:
            continue
        touched.add(id(target))
        sources = target[1:]
        if "'none'" in sources:
            target.remove("'none'")
        if origin not in sources:
            target.append(origin)
        # Only the directive that actually governs scripts gets a nonce; a
        # nonce elsewhere could disable 'unsafe-inline' for styles.
        if name.startswith("script-src") and _needs_nonce(sources):
            target.append(f"'nonce-{nonce}'")
            use_nonce = True
    return "; ".join(" ".join(d) for d in directives), use_nonce


def extend_csp_headers(headers, origin: str, nonce: str) -> bool:
    """Extend every Content-Security-Policy header in place (a mitmproxy
    Headers object or a dict of lists). Returns whether a nonce is needed."""
    use_nonce = False
    values = headers.get_all("Content-Security-Policy") if hasattr(headers, "get_all") \
        else headers.get("Content-Security-Policy", [])
    if not values:
        return False
    new = []
    for value in values:
        # One header can carry several comma-separated policies; each is
        # enforced on its own, so each must allow the script.
        policies = []
        for policy in value.split(","):
            extended, needs = extend_policy(policy, origin, nonce)
            policies.append(extended)
            use_nonce |= needs
        new.append(", ".join(policies))
    if hasattr(headers, "set_all"):
        headers.set_all("Content-Security-Policy", new)
    else:
        headers["Content-Security-Policy"] = new
    return use_nonce


def script_tag(origin: str, nonce: str | None) -> bytes:
    attr = f' nonce="{nonce}"' if nonce else ""
    return f'<script src="{origin}/inject.js"{attr} {MARKER.decode()}></script>'.encode()


def inject_html(body: bytes, tag: bytes) -> bytes | None:
    """Insert `tag` as early as possible: right after <head>, else after
    <html>, else after the doctype. None if the page already has it, or if
    it isn't a full document (an HTML fragment fetched by script, where a
    prepended tag could break the page that inserts it)."""
    if MARKER in body[:65536]:
        return None
    for pattern in (HEAD, HTML, DOCTYPE):
        m = pattern.search(body, 0, 65536)
        if m:
            return body[:m.end()] + tag + body[m.end():]
    return None


def new_nonce() -> str:
    return secrets.token_urlsafe(18)
