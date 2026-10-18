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

Edit `config/config.yaml` and restart the container. It only needs the keys
you change; every option and its default is documented in
[`censor_proxy/defaults.yaml`](censor_proxy/defaults.yaml). Unknown keys are
rejected at startup.

The detection log is `logs/detections.jsonl`. At `logging.level: info` it
records hostnames; `debug` adds full URLs and every image scanned.

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
xfail.
