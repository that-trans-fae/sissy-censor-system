# Censor proxy

A mitmproxy-based HTTP/HTTPS proxy that blurs female explicit regions in images
before the browser renders them. See [`../docs`](../docs) for the handoff,
the [design decisions](../docs/decisions.md) and [status](../docs/status.md).

## Run

```bash
docker compose up -d --build
```

The proxy listens on `127.0.0.1:8080`. On first start mitmproxy creates its CA
in `./mitmproxy/`, which is kept across rebuilds so trust only has to be
installed once. Check that it's up:

```bash
curl -x http://127.0.0.1:8080 http://censor.local/health
```

## Point the system at it

Use the PAC file rather than a fixed proxy so browsing falls back to a direct
connection when the container is stopped (not yet tested; Chromium may refuse
a `file://` PAC URL from system settings):

```bash
gsettings set org.gnome.system.proxy mode auto
gsettings set org.gnome.system.proxy autoconfig-url "file://$PWD/proxy.pac"
```

To undo: `gsettings set org.gnome.system.proxy mode none`.

## Trust the CA

System store (curl, most non-browser apps):

```bash
sudo cp mitmproxy/mitmproxy-ca-cert.pem /usr/local/share/ca-certificates/mitmproxy.crt
sudo update-ca-certificates
```

### Chromium (snap)

Trust the CA with the `CACertificates` policy, which covers every profile.
Adding it to the snap's NSS database with `certutil` is not enough: Chromium
lists it as an intermediate, not a trusted root. Generate the policy from your
CA and install it:

```bash
printf '{\n  "CACertificates": ["%s"]\n}\n' "$(openssl x509 -in mitmproxy/mitmproxy-ca-cert.pem -outform DER | base64 -w0)" > mitmproxy/chromium-policy.json
```

```bash
sudo mkdir -p /var/snap/chromium/current/policies/managed
```

```bash
sudo cp mitmproxy/chromium-policy.json /var/snap/chromium/current/policies/managed/censor-proxy.json
```

Restart Chromium and check `chrome://policy` shows `CACertificates` as OK. To
try it without changing system settings, use a separate test profile:

```bash
chromium --user-data-dir=$HOME/snap/chromium/common/censor-test --proxy-server=127.0.0.1:8080
```

Chromium's component updater ignores the policy, so `update.googleapis.com`
and `optimizationguide-pa.googleapis.com` are in the passthrough list in
`config/config.yaml`.

### Firefox (snap)

Still to do.

## Configure

Use the [admin panel](#admin-panel), or edit `config/config.yaml` and restart
the container (or press "Reload from file" in the panel). It only needs the
keys you change; every option and its default is documented in
[`censor_proxy/defaults.yaml`](censor_proxy/defaults.yaml). Unknown keys are
rejected at startup.

The detection log is `logs/detections.jsonl`. At `logging.level: info` it
records hostnames; `debug` adds full URLs, every image scanned, and every
response the browser requested as an image that wasn't scanned (action
`unscanned`, with the reason). To find what is slipping through:

```bash
grep '"unscanned"' logs/detections.jsonl
```

## Video

On every site (narrow it with `video.hosts`) every video is paused and
covered by an injected control. "Play censored" has the proxy fetch the
video, blur it with ffmpeg and stream the censored copy back; playback
starts after a few seconds of video are ready. Videos the control can't find
a source for stay covered. The control's text and colours are at the top of
`censor_proxy/static/inject.js`; its name is `video.brand_name`.

## Admin panel

Open either:

- <http://localhost:8082/admin>: direct, published on the host's loopback only
- <https://scs.local/admin>: through the proxy (`proxy.admin_host`)

Tabs:

- **Overview**: counts since start (censored, clean, whitelisted, failed
  closed...), the model and the log level.
- **Whitelist**: domains that pass through unscanned and unmodified,
  subdomains included. Pages from them also get no video control. By default
  what a whitelisted page loads passes too (matched by `Referer`), so
  YouTube's thumbnails from `i.ytimg.com` aren't scanned. Turn that off with
  the checkbox (`whitelist.match_referer`). `youtube.com` is in the example
  config.
- **Detections**: the latest entries of the detection log, filterable. No
  images, just hosts, labels and scores.
- **Video**: video jobs with their progress, and a button to cancel them.
- **Settings**: two switches, plus every option in Custom mode.
  - **Image model**: Fast (320n) or Slow (640m), one choice per entry in
    `model.available` (its `label`). The video model has its own dropdown,
    because 640m can't keep up with video.
  - **Censoring level**: Strict (the detection defaults), Lax (exposed
    breasts pass) or Custom.

  Custom shows every option in `defaults.yaml` with its comment, starting
  from the level you had; the other levels hide them and fix the detection
  settings to the level's values. Saving writes `config/config.yaml`,
  keeping its comments, and applies the change at once. Options marked
  "restart" (ports, the video cache and slots) take effect after
  `docker compose restart`. Add as many levels as
  you like under `detection.presets` in `config/config.yaml`, each with a
  `description` and the detection `settings` it changes. They merge with the
  built-in ones by name (`lax: null` removes one), and each appears as a
  choice:

  ```yaml
  detection:
    presets:
      genitals-only:
        description: Only exposed genitalia are censored.
        settings:
          female_labels: [FEMALE_GENITALIA_EXPOSED]
          unsexed_labels: []
  ```

- **PIN lock**: an optional 4-12 digit PIN, stored hashed as
  `admin.pin_hash`. While locked, the whole panel shows only the PIN prompt.
  A wrong PIN never locks anyone out. Each one is recorded in
  `logs/admin-attempts.jsonl` (`admin.attempts_log`) with its time, the
  address used and the browser. After the next unlock they're listed at the
  top of the panel until dismissed. An unlocked panel locks again after 15
  idle minutes.

Any page you visit can send requests to both addresses, so the panel refuses
other `Host` names (DNS rebinding) and any write that isn't JSON from its own
origin, and sends no CORS headers. `config/` is mounted writable in
`compose.yaml` so the panel can save.

## Review detections

Draws every detection on each sample (red = censored, grey = passed) next to
the proxy's actual output, and writes `review/index.html` and
`review/detections.json`. It uses the default model (320n) unless given a
config; this reviews with 640m:

```bash
mkdir -p ../review && printf 'model: {path: /app/models/640m.onnx, resolution: 640}\n' > ../review/config.yaml
```

```bash
docker run --rm -u "$(id -u):$(id -g)" -v "$PWD/..:/w" -w /w/src censor-proxy \
  python tools/draw_boxes.py /w/docs/samples /w/review --config /w/review/config.yaml
```

Once the boxes are confirmed, copy `review/detections.json` to
`tests/fixtures/expected-<model>.json` (`640m` or `320n`) to make it that
model's regression fixture; `tests/test_samples.py` checks each fixture with
its own model, whatever the configured default.

## Test

```bash
docker run --rm -v "$PWD/..:/w" -w /w/src censor-proxy \
  sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q -p no:cacheprovider"
```

Rebuild the image first if `requirements.txt` changed; the code itself is
mounted. Unit tests use a fake detector and need no weights.
`tests/test_samples.py` runs each model against its confirmed fixture and needs
the sample images in `docs/samples/` (not in git); known failures show as
xfail. 640m animation cases take ~90 s each and are skipped unless
`CENSOR_SLOW_TESTS=1` is set (`docker run -e CENSOR_SLOW_TESTS=1 ...`).
