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
- Animation lane (Oct 19, 2026): timed-out scans stop at their deadline
  instead of holding a worker, and animations leave `images.still_workers`
  workers free for still images (found on redtube.com, where thumbnails
  waited behind Reddit GIFs from another tab)
- Scene cuts in animations (Oct 19, 2026): frames that cut to a new picture
  are always scanned instead of borrowing their neighbours' boxes (a
  slideshow ad showed breasts on its unscanned frames)
- Page-facing hardening (Oct 19, 2026): no CORS on `/health` and
  `/inject.js`, job endpoints answer only allowlisted origins, no named
  global or script tag left in the page, Referer from the `Origin` header,
  a queue cap and per-origin rate limit on job starts, and an ffmpeg
  protocol whitelist. Tested with pytest only, not yet in a browser
- Redtube player (Oct 19, 2026): video cards stack above the player's
  own overlays but under page modals, ad videos in `srcdoc`/`about:blank`
  frames are paused and covered, manifests listed in JSON API responses are
  found, and preroll ads (`video.ad_sources`) are never taken as the source.
  Checked headlessly through the proxy (card topmost over the whole player,
  srcdoc ad paused, four manifests listed) and confirmed playing in the
  browser
- Pornhub prerolls (Oct 19, 2026): media files named in VAST ads the
  page fetches (XML `<MediaFile>` or JSON `mediaFiles`) are never taken as
  a video's source, so the page's own manifest is used instead of the ad.
  Scanning checked against the real ad response, and confirmed playing
  the video in the browser
- Injection only before the page's first `<body>`/`<script>`, no body
  opt-out marker, the admin panel's Video tab explains the injection
  trade-off, and the compose service runs read-only with no capabilities
  (Oct 19, 2026); the hardened container was started and exercised with
  curl. Then: random per-origin job ids, the debug beacon gated on origin,
  and the service running as an unprivileged user with the confdir at
  `/mitmproxy` (needs `docker compose up -d --build`)
- Censored videos start muted (`video.start_muted`, on by default, a
  toggle in the admin panel's Video settings). Unit-tested only, not yet
  checked in a browser
- Admin panel at `http://localhost:8082/admin` and `https://scs.local/admin`:
  status, detection log, video jobs, whitelist (built-in hosts, YouTube
  included, plus your own domains), censoring levels (Full, Cocks only, Only Women, Permissive, custom; any number of
  presets, each listing what it censors), a Fast/Slow image model switch, a resource consumption
  switch (Minimal, Recommended, Maximum, custom), every setting editable in custom
  and applied live, and an optional PIN lock that reports wrong entries
  instead of locking out. Checked in a browser against a test container (whitelist, settings,
  PIN, phone layout). `https://scs.local` was only tested with curl through
  the proxy

- Installer (`install/`): one-line scripts run from GitHub for Linux,
  macOS and Windows (Docker Engine on Linux, Docker Desktop elsewhere,
  clone, build, with `SCS_UNINSTALL=1` to reverse) that set up a dedicated "Sissy Browser" Chromium profile
  pinned to the proxy with its own icon, the admin panel on its bookmark
  bar and the proxy CA trusted in the profile's own certificate store (no
  flags, no system trust). Verified on Ubuntu with snap Chromium 155; macOS
  and Windows are syntax-checked only (bash, shellcheck, PowerShell
  parser). The system-level installers were dropped and Linux moved from
  Docker Desktop to Docker Engine (Oct 19, 2026); the Engine path is
  syntax-checked only. The
  repository URL is still a placeholder

- Distribution and automatic updates (Oct 19, 2026): the installers pull
  the image from Docker Hub and copy its `dist/` files out (no clone, no
  build), and a host job (`scs-update`) checks once at each startup and
  installs a new release once the Sissy Browser is closed, keeps them once the image's health check
  passes, and rolls back and skips them otherwise. The result and an on/off
  switch are on the panel's Overview; `tools/release.sh` publishes amd64
  and arm64. Verified on Linux against a local registry with three
  releases (pending while the browser ran, applied after, a broken release
  rolled back and skipped, the panel switch) and with pytest against a
  fake docker. Not yet: a real push to Docker Hub (`thattransfae/scs`) or
  an arm64 build; the systemd timer, launchd
  agent and Windows task are written but untried, and the PowerShell
  updater is parse-checked only

## Not done yet

- **Video**: built and tested end to end with curl (injection, CSP, jobs),
  injected on every site. Seeking anywhere while a job runs (segments
  through Media Source Extensions, the job restarting where the viewer
  seeks) is tested in Chromium against local MP4 and HLS sources; on a
  real streaming site a far seek into a long video looped restarting
  (fixed Oct 19, 2026; needs a retest there). HLS plays in
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
