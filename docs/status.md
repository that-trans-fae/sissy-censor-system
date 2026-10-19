# Status

Where the project stands against the handoff plan. Update this when a piece
of work lands or the plan changes.

Last updated Oct 18, 2026.

## Done (Phase 1)

- Docker image (`src/Dockerfile`, `src/compose.yaml`) with mitmproxy, NudeNet
  320n (default) and 640m weights baked in
- `censor.py` addon: Content-Type routing, passthrough hosts, size floor,
  `censor.local/health`, fail-closed on errors and timeouts
- `censor_bytes()` for JPEG, PNG, WebP and animated GIF/WebP, with frame
  sampling and a content-hash cache
- Female-only selection rule with context thresholds (see `decisions.md`)
- YAML config over documented defaults; JSONL detection log
- Box-draw review tool, unit tests, per-model sample fixtures
- Chromium (snap): CA trusted via policy, verified on live HTTPS in a test
  profile
- Data URIs: base64 images in HTML, CSS and JSON (including JSON-escaped
  slashes) are censored in place through the same `censor_bytes()`
- Mislabelled and embedded images: images sniffed by signature when the
  Content-Type doesn't say image (octet-stream, missing, or anything
  requested as an image); data URIs in JavaScript, XML, SVG and plain text,
  with parameters and any case, including images inside base64 SVGs;
  `unscanned` debug log entries for image requests that weren't scanned
- Recall tuning: tiling of tall/wide images and lower thresholds (false
  positives preferred to misses)
- Branding: mark, palette and README banners in `docs/brand/`; the video
  overlay restyled to match (checked in a browser on a static test page)

## Not done yet

- **Video**: built and tested end to end with curl (injection, CSP, jobs,
  live fMP4 streaming, Range), injected on every site. HLS plays in
  Chromium on Pornhub (manifest from inline page config, browser-like
  ffmpeg requests); cancelling closed, paused and abandoned jobs is verified with curl only.
  DASH still untested against a real site
- **System-wide proxy**: GNOME proxy setting with `src/proxy.pac`, and a check
  that browsing falls back to direct when the container is down. Chromium may
  not accept a `file://` PAC URL from system settings; if not, serve it over
  HTTP.
- **Firefox (snap)**: CA trust and proxy settings
- **Tuning from real browsing**: thresholds were tuned on 15 samples; review
  `src/logs/detections.jsonl` for misses and false positives

- **Security warning**: tell the user that video injection extends every
  page's CSP and lets pages ask the proxy to fetch public video URLs

## Later phases (from the handoff)

- Phase 3: InsightFace fallback for covered-but-suggestive content (out of
  scope for now: exposed anatomy only)
- Phase 4: DOM observer for canvas and JS-built images; only if real browsing
  shows sites slipping through
