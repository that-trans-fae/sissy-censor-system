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
src/censor_proxy/config.py     dataclasses + loader (defaults.yaml merged with user file)
src/censor_proxy/defaults.yaml every option, documented
src/censor_proxy/detection_log.py  JSONL detection log
src/config/config.yaml         user overrides, mounted at /config
src/tools/draw_boxes.py        box-draw review of the samples
src/tests/                     pytest; fixtures/expected-<model>.json
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
dependency or Dockerfile changes do. Restart the compose container after
editing code or `config/config.yaml`.

Docker is Docker Desktop (a VM): bind mounts must be under `/home`, so
`/tmp` and the scratchpad can't be mounted. Pipe throwaway scripts in on
stdin, or put them under `src/` and delete them afterwards.

## How detection works

- `Censor.censor_bytes()` is the only path into detection; every caller
  (image responses now, data URIs next) goes through it.
- `decide()` in `detection.py` holds the whole selection rule. Female labels
  use context-dependent thresholds; unsexed labels are attributed by
  proximity to female or male context. `docs/decisions.md` explains each
  threshold and the sample behind it.
- Default model is the bundled 320n (fast). 640m is baked into the image at
  `/app/models/640m.onnx` and is more accurate; switch with `model.path` and
  `model.resolution`.

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
- The addon's `responseheaders` hook runs after mitmproxy's StreamBodies
  addon and turns streaming back off for images it will scan.
- Chromium (snap) only trusts the proxy CA through the `CACertificates`
  policy; the NSS database route registers it as an intermediate. Its
  updater hosts are in the passthrough list because they ignore the policy.
- `src/mitmproxy/` holds the CA private key: never commit or share it.
- `docker ps` truncates commands, so grepping its output for a script name
  silently matches nothing; use `--no-trunc` (or `--filter`) before killing
  a background container.
