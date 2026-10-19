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
and `optimizationguide-pa.googleapis.com` are in the default passthrough list
(`proxy.passthrough_hosts`).

### Firefox (snap)

Still to do.

## Configure

Edit `config/config.yaml` and restart the container. It only needs the keys
you change; every option and its default is documented in
[`censor_proxy/defaults.yaml`](censor_proxy/defaults.yaml). Unknown keys are
rejected at startup.

### Passthrough hosts

Hosts matching `proxy.passthrough_hosts` bypass TLS interception, so nothing
they serve is censored. The defaults cover Chromium's updaters, sign-in
providers, password managers, payment processors, common US banks and
brokerages, and every `.gov`, `.mil` and `.edu` site. Add your own (your bank,
health portal, employer or university sign-in) with
`proxy.extra_passthrough_hosts`, which is appended to the defaults; setting
`passthrough_hosts` itself replaces them. Only add hosts that never serve
images you'd want censored.

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

## Image placeholders

The proxy holds each image until it is censored, so on image-heavy pages
images queue behind each other. The same injected script covers any image
still loading 400 ms after it comes into view (`images.placeholder_delay_ms`)
with a "Censoring" card until it arrives. Images smaller than 48 px on the
page get none (`images.placeholder_min_px`). Turn it off with
`images.placeholders: false`. The page can't tell a censor queue from a slow
network, so slow images show the card too.

This covers every `<img>`, GIFs included (animated GIFs are the slowest to
censor, often 10-20 s). An image the page gives no size takes no room while
it loads; the card then covers the sized box it sits alone in, or else the
image is held open at 160 px (`images.placeholder_reserve_px`) until it
arrives. Not covered: CSS background images (the page can't see which are
still loading), images opened directly in their own tab (there is no page to
inject into), and "GIFs" that are really videos, which get the video cover
instead.

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
