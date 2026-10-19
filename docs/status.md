# Status

Where the project stands against the handoff plan. Update this when a piece
of work lands or the plan changes.

Last updated Oct 19, 2026.

## Done (Phase 1)

- Docker image (`src/Dockerfile`, `src/compose.yaml`) with mitmproxy, NudeNet
  320n (default) and 640m weights baked in
- `censor.py` addon: Content-Type routing, the whitelist (built-in hosts plus
  your domains, never intercepted), size floor,
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
- Image placeholders: the injected script covers images still loading
  (queued at the censor) with a "Censoring" card. Checked in a browser on a
  static test page with a simulated queue (alignment, rounded corners,
  clipping in a scroller, sticky header on top, click-through, cleared on
  load; unsized GIFs held open and released, tracking pixels and icons left
  alone); not yet tried on real sites. Not covered: CSS background images,
  images opened directly in a tab
- Queue bypass: images that need no detection (small, unsupported, cached),
  and text bodies whose data URIs are all such images, no longer wait for a
  detection worker
- Page-facing hardening (Oct 19, 2026): no CORS on `/health` and
  `/inject.js`, job endpoints answer only allowlisted origins, no named
  global or script tag left in the page, Referer from the `Origin` header,
  a queue cap and per-origin rate limit on job starts, and an ffmpeg
  protocol whitelist. Tested with pytest only, not yet in a browser
- Injection only before the page's first `<body>`/`<script>`, no body
  opt-out marker, the admin panel's Video tab explains the injection
  trade-off, and the compose service runs read-only with no capabilities
  (Oct 19, 2026); the hardened container was started and exercised with
  curl. Then: random per-origin job ids, the debug beacon gated on origin,
  and the service running as an unprivileged user with the confdir at
  `/mitmproxy` (needs `docker compose up -d --build`)
- Admin panel at `http://localhost:8082/admin` and `https://scs.local/admin`:
  status, detection log, video jobs, whitelist (built-in hosts, YouTube
  included, plus your own domains), censoring levels (Full, Cocks only, Only Women, Permissive, custom; any number of
  presets, each listing what it censors), a Fast/Slow image model switch, every setting editable in custom
  and applied live, and an optional PIN lock that reports wrong entries
  instead of locking out. Checked in a browser against a test container (whitelist, settings,
  PIN, phone layout). `https://scs.local` was only tested with curl through
  the proxy

- Installers (`install/`): one-line scripts run from GitHub for Linux,
  macOS and Windows (Docker Desktop, clone, build, system CA trust, system
  proxy, with `SCS_UNINSTALL=1` to reverse) and a dedicated "Sissy Browser"
  Chromium profile pinned to the proxy with its own icon, the admin panel
  on its bookmark bar and the proxy CA trusted in the profile's own
  certificate store (no flags, no system trust). The browser installer is
  verified on Ubuntu with snap Chromium 155; the system-level installers,
  macOS and Windows are syntax-checked only (bash, shellcheck, PowerShell
  parser). The repository URL is still a placeholder

## Not done yet

- **Video**: built and tested end to end with curl (injection, CSP, jobs),
  injected on every site. Seeking anywhere while a job runs (segments
  through Media Source Extensions, the job restarting where the viewer
  seeks) is tested in Chromium against local MP4 and HLS sources only; not
  yet on a real streaming site. HLS plays in
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

- **Male subjects read as female in video**: a 437 s vertical video of a
  male subject (Pornhub viewkey 6710b29dca308) had 8,496 frames censored,
  all genuine `FEMALE_BREAST_EXPOSED` detections (median 0.44, max 0.76;
  male breast score under 0.26) in half the samples. Per-frame rules can't
  fix it: a penis or male face is in frame in only ~5% (320n) / ~35% (640m)
  of those samples, and 200 of 1,244 censored-breast images in the browsing
  log also show a penis. Video-level attribution (`video_attribution`, see
  `decisions.md`) is built and tested, and replayed on that video (813
  censored samples to 188, or 12 at a 0.45 female threshold), but ships
  off: it needs straight videos with women (must stay censored) and other
  male-subject videos (must pass) dumped with `tools/dump_video_scores.py`
  and replayed with `tools/replay_attribution.py` before it is turned on.
  Not yet tried in the live proxy


## Later phases (from the handoff)

- Phase 3: InsightFace fallback for covered-but-suggestive content (out of
  scope for now: exposed anatomy only)
- Phase 4: DOM observer for canvas and JS-built images; only if real browsing
  shows sites slipping through
