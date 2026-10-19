# CLAUDE.md

A local mitmproxy-based HTTP/HTTPS proxy, run in Docker, that blurs female
explicit regions in images before the browser renders them. Male-labelled
regions pass through; images are always modified in place, never blocked or
replaced. Everything runs locally: no external API calls, no telemetry.

## Read first

- `docs/Image Censoring Proxy — Project Handoff.md`: the original plan.
  Historical; where it disagrees with the files below, they win.
- `docs/decisions.md`: settled design decisions and detection tuning, with
  the samples that motivated each rule. Add a row when a decision changes.
- `docs/status.md`: what's done and what's next. Keep it current.
- `src/README.md`: run, browser setup, configure, review, test.

## Layout

```
src/censor.py                  mitmproxy addon (entry point, mitmdump -s)
src/censor_proxy/imaging.py    Censor.censor_bytes(): decode, detect, blur, re-encode
src/censor_proxy/detection.py  NudeNet wrapper (with tiling) and decide(), the selection rule
src/censor_proxy/datauri.py    base64 data-URI images in HTML/CSS/JSON bodies
src/censor_proxy/video.py      video jobs: ffmpeg decode -> detect -> blur -> fMP4
src/censor_proxy/server.py     internal HTTP server behind https://censor.local
src/censor_proxy/inject.py     HTML script injection and CSP extension
src/censor_proxy/admin.py      admin panel server (localhost:8082, scs.local): PIN, config edits
src/censor_proxy/static/inject.js  the injected video control and image placeholders (branding at the top)
src/censor_proxy/static/admin.*    the admin panel page
src/censor_proxy/config.py     dataclasses + loader (defaults.yaml merged with user file)
src/censor_proxy/defaults.yaml every option, documented
src/censor_proxy/detection_log.py  JSONL detection log
src/config/config.yaml         user overrides, mounted at /config
src/tools/draw_boxes.py        box-draw review of the samples
src/tests/                     pytest; fixtures/expected-<model>.json
docs/brand/                    mark, README banners, palette
docs/samples/{censor,mixed,uncensor}/  sample images (not in git)
```

## Commands

Everything runs in the Docker image; the host Python is not used. Run from
`src/`:

```bash
docker build -t censor-proxy .
```

```bash
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD/..:/w" -w /w/src censor-proxy sh -c "pip install -q --user -r requirements-dev.txt; python -m pytest -q -p no:cacheprovider"
```

```bash
docker compose up -d --build
```

The sample tests run both models over every sample (~2 minutes). 640m on
animations takes ~90 s each, so those cases are skipped unless
`-e CENSOR_SLOW_TESTS=1` is passed to `docker run`; run them before freezing
a 640m fixture. Add `--deselect tests/test_samples.py` for a quick unit-test
run.

The test command mounts the working tree, so code edits don't need a rebuild;
dependency or Dockerfile changes do. The compose container runs the code
baked into the image, so rebuild it (`docker compose up -d --build`) after
editing code; a restart is enough for `config/config.yaml`.

Docker is Docker Desktop (a VM): bind mounts must be under `/home`, so
`/tmp` and the scratchpad can't be mounted. Pipe throwaway scripts in on
stdin, or put them under `src/` and delete them afterwards.

## How detection works

- `Censor.censor_bytes()` is the only path into detection; every caller
  (image responses now, data URIs next) goes through it. The addon first
  calls `Censor.precheck()` off the worker pool, which settles images that
  need no detection (small, unsupported, cached) so they don't queue.
- `decide()` in `detection.py` holds the whole selection rule. Female labels
  use context-dependent thresholds; unsexed labels are attributed by
  proximity to female or male context. `docs/decisions.md` explains each
  threshold and the sample behind it.
- Default model is the bundled 320n (fast). 640m is baked into the image at
  `/app/models/640m.onnx` and is more accurate; switch with `model.path` and
  `model.resolution`.

## How video works

On pages whose host matches `video.hosts` (every site by default), the
addon injects
`https://censor.local/inject.js` at the top of `<head>` and extends the CSP
to allow it. The script covers and pauses every `<video>` (patching
`play()` and `attachShadow`, so shadow-DOM players are caught too) and, on
click, posts the source URL to `/video/jobs`. Only full documents and
iframes are injected (`Sec-Fetch-Dest`), never fetched HTML fragments. The addon routes
`censor.local` to the internal server on `proxy.internal_port`; jobs run
ffmpeg in `video.py` and stream fragmented MP4 back while they run. Job
requests are only accepted from origins matching `video.hosts`, and only
for public addresses (`video.allow_private_sources` relaxes that). See
`docs/video-design.md`.

The same script draws image placeholders (`images.placeholders`): an
`<img>` still loading a moment after it comes into view gets a "Censoring"
card until it loads. It is injected while either feature is on, and gates
its video hooks on `video.enabled`. The pieces are filled into the script by
`InternalServer.inject_js()` (`__NAME__` markers).

## Admin panel

`admin.py` runs its own listener on `admin.port` (8082). Compose publishes
it on `127.0.0.1`, and the addon routes `proxy.admin_host` (`scs.local`)
there, keeping the `Host` header. The panel writes `config.yaml` through
ruamel.yaml, keeping comments, then calls the addon's `apply_config()`, which
swaps the config on the event loop. A new config key gets a form field
automatically (typed from its default, with help text from the comment above
it in `defaults.yaml`). If the key is only read at startup, add it to
`RESTART_KEYS`. Whitelisted flows (`whitelist.domains`, matched on host or
Referer) are tagged in `request()` and streamed untouched.

## Working conventions

- **Config over constants.** Any behaviour choice gets a config key with the
  chosen default. Adding a key means updating `defaults.yaml` (with a
  comment), the dataclass and `_build()` in `config.py`. Unknown keys are
  rejected, so tests catch drift. More detailed output goes behind a more
  verbose log level (`debug`), not on by default.
- **Detection changes need the sample review.** After changing `decide()`,
  thresholds or the model, run `tools/draw_boxes.py` (pass `--config` for
  640m; it uses the default model otherwise), check every sample against its
  folder (`censor/` and `mixed/` censored, `uncensor/` clean) for both
  models, and ask the user to confirm new or changed boxes by eye before
  rewriting a fixture. Never freeze unconfirmed boxes. Samples a model gets
  wrong stay in its fixture as strict xfails, with the correct result and
  the reason (the detections that fell short).
- **Sample images are explicit.** Don't view them, don't commit them, and
  never publish review output as an artifact or share it anywhere. Work
  from detection scores and let the user do the visual review.
- **The user runs system and trust changes.** Installing CAs, writing
  browser policies, changing proxy settings or anything needing sudo: give
  the commands and let the user run them.
- **Commits** only when asked, on `main`, message ending with the
  `Co-Authored-By` line. There is no remote.

## Gotchas

- NudeNet's `detect()` expects a BGR array; frames are kept RGB(A) and
  converted just for detection.
- `NudeDetector` ignores its `providers` argument and has no session options,
  so `Detector` replaces its ONNX session to set thread counts.
- Never set mitmproxy's `stream_large_bodies`: it is applied after the
  addon's `responseheaders` hook and silently streams large images past the
  censor. The addon decides streaming itself (buffer what it inspects,
  stream everything else); `tests/test_proxy.py` guards this.
- Chromium (snap) only trusts the proxy CA through the `CACertificates`
  policy; the NSS database route registers it as an intermediate. Its
  updater hosts are in the passthrough list because they ignore the policy.
- `src/mitmproxy/` holds the CA private key: never commit or share it.
- `docker ps` truncates commands, so grepping its output for a script name
  silently matches nothing; use `--no-trunc` (or `--filter`) before killing
  a background container.
