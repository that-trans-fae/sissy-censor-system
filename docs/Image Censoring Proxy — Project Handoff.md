# Image Censoring Proxy — Project Handoff

Oct 18, 2026

## Project overview

The deliverable is a single Docker container running a system-wide HTTP/HTTPS proxy that detects explicit images and GIFs in transit and blurs the explicit regions before the browser renders them. Images and GIFs are always modified in place and delivered, never blocked, dropped or swapped for a placeholder; the rest of the picture stays intact. Detection and censoring are restricted to female subjects; male-labelled regions pass through unmodified. Everything in the stack is open source and runs locally with no external API calls.

**In scope**

- Still images (JPEG, PNG, WebP) and animated GIFs served over HTTP or HTTPS
- Images inlined as base64 data URIs inside HTML, CSS and JSON responses
- A configurable passthrough list for hosts that cannot be intercepted
- Logging of every detection with confidence scores for later tuning

**Out of scope for v1**

- Video (MP4, WebM) and HTTP Live Streaming
- Images built client-side by JavaScript or drawn on a canvas (addressed by an optional Phase 4)
- Non-browser traffic that pins certificates (mobile apps, Electron apps)
- Training or fine-tuning any model; v1 uses pretrained weights only

## Architecture

The system is a man-in-the-middle proxy: because nearly all image traffic is HTTPS, the proxy must terminate TLS with its own certificate authority, which the host OS is configured to trust. Every response is routed by Content-Type into one of two branches, and both branches call the same `censor_bytes(data, mime)` function so detection logic lives in exactly one place.

&#91;embedded content: proxy architecture · 2 branches, 1 censor function\]

Image responses are censored directly; text bodies are scanned for base64 data URIs, which are decoded, censored and re-encoded in place. Hosts on the passthrough list bypass TLS interception entirely. Every detection is appended to a JSONL log with its labels and confidence scores.

## Technology stack

All components are open source and install with `pip`; the only non-Python dependency is FFmpeg if video support is added later.

| Layer | Component | Role | Why this one |
| --- | --- | --- | --- |
| Proxy | [mitmproxy](https://mitmproxy.org/) (`mitmdump -s censor.py`) | TLS termination, response interception, addon runtime | Python addon API means the ML pipeline runs in-process; generates its own CA |
| Fast pre-filter | [OpenNSFW2](https://github.com/bhky/opennsfw2) | Whole-image NSFW score | Cheap gate so the detector only runs on candidates |
| Region detector | [NudeNet](https://github.com/notAI-tech/NudeNet) (ONNX) | Bounding boxes with sex-specific labels (`FEMALE_BREAST_EXPOSED`, `FEMALE_GENITALIA_EXPOSED`, etc.) | Labels already separate female and male anatomy, which is the core selection rule |
| Gender fallback | [InsightFace](https://github.com/deepinsight/insightface) | Face detection with sex attribute | Only if clothed-but-suggestive content must also be gated; deferred to Phase 3 |
| Inference runtime | onnxruntime (CPU) or onnxruntime-gpu | Runs NudeNet and InsightFace models | GPU recommended for GIF frame throughput |
| Image ops | OpenCV (`opencv-python-headless`), Pillow | Decode, Gaussian blur or pixelate regions, re-encode; GIF frame iteration | Headless build keeps the image small |
| HTML/CSS parsing | selectolax, regex | Find `<img src>`, `srcset` and `data:image/...;base64` URIs | selectolax is far faster than BeautifulSoup for per-response parsing |
| Container | `python:3.11-slim` (CPU) or `nvidia/cuda` base (GPU) | Packaging | Slim keeps the CPU image under \~1.5 GB with models |
| Config | YAML or environment variables | Thresholds, passthrough hosts, blur style, size floor | Tunable without rebuilding |

## Processing pipeline

The addon's `response()` hook runs once per HTTP response. Three paths lead into the shared `censor_bytes()` function.

**Image responses** (`Content-Type: image/*`)

1. Skip if the host is on the passthrough list or the body is under the size floor (default 4 KB).
2. Read `flow.response.content` (mitmproxy has already decoded gzip/brotli).
3. If GIF or animated WebP, split into frames with Pillow; otherwise treat as a single frame.
4. Per frame: run OpenNSFW2. Below the pre-filter threshold (default 0.3) the frame passes untouched.
5. Above threshold: run NudeNet, keep only boxes whose label starts with `FEMALE_` and whose score exceeds the detection threshold (default 0.5).
6. Apply the configured censor (Gaussian blur, pixelate or solid fill) to each kept box with OpenCV, padding the box by 10%.
7. Re-encode in the original format; reassemble GIF frames preserving durations and loop count.
8. Write the result back to `flow.response.content` and append a log line with host, path, labels and scores.

**Text bodies** (`text/html`, `text/css`, `application/json`)

1. Regex-scan for `data:image/(png|jpeg|gif|webp);base64,<payload>`.
2. For HTML, also parse `<img src>`, `srcset` and inline `style` attributes with selectolax to catch URIs the regex misses.
3. Decode each match, call `censor_bytes()`, re-encode, and substitute the new URI in place.
4. Raise `stream_large_bodies` for these content types so large pages are not skipped.

**Censor helper** `censor_bytes(data: bytes, mime: str) -> bytes` wraps steps 3 to 7 above so every caller gets identical behaviour and thresholds.

## System integration

The container exposes port 8080; the host is pointed at it in one of two ways, and the proxy's CA is trusted system-wide either way.

| Concern | Approach | Notes |
| --- | --- | --- |
| Container run | `docker run -p 8080:8080 -v ./mitmproxy:/root/.mitmproxy -v ./config:/config censor-proxy` | Volume keeps the CA stable across rebuilds; otherwise every rebuild needs a new trust install |
| Entry point | `mitmdump -s /app/censor.py --set stream_large_bodies=5m --set confdir=/root/.mitmproxy` | Raise `stream_large_bodies` per content type inside the addon for HTML/JSON |
| Explicit proxy (simplest) | Set OS HTTP and HTTPS proxy to `localhost:8080` | Honoured by browsers, curl, most desktop apps; apps that ignore proxy settings are missed |
| Transparent proxy (catch-all) | `mitmdump --mode transparent` plus `iptables`/`nftables` REDIRECT of TCP 80 and 443 (Linux) or `pf` rdr rules (macOS) | Catches everything, needs `--network host` or a separate network namespace; Windows needs a TUN-based approach |
| CA trust | Install `~/.mitmproxy/mitmproxy-ca-cert.pem` into the OS trust store; separately into Firefox, which keeps its own store | Visiting `http://mitm.it` through the proxy serves the cert for each platform |
| HTTP/3 | Block outbound UDP 443 at the firewall | Forces browsers to fall back to HTTP/2 over TCP, which the proxy can intercept |
| Passthrough | `ignore_hosts` option with a regex list from config | Required for certificate-pinned clients (banking apps, Electron apps, OS update services) |
| Health check | `GET http://censor.local/health` served by the addon | Returns model load state and queue depth; lets the OS proxy setting fail open if the container is down |

## Known limitations and risks

The two risks most likely to shape the user experience are latency on image-heavy pages and the accuracy of the female-only selection rule; both are tunable but neither can be eliminated.

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Gender inference errors | NudeNet's `FEMALE_`/`MALE_` labels and InsightFace's sex attribute misclassify partial bodies, stylised art and androgynous subjects, so content is both missed and over-censored | Make thresholds configurable; log every decision with scores; tune on a local sample set; consider a "when unsure, censor" mode |
| Latency | Every candidate image runs through at least one model before the browser sees it; GIFs multiply this per frame | OpenNSFW2 gate with a size floor; GPU runtime; a frame-sampling mode for GIFs (classify every Nth frame, apply boxes to neighbours); response cache keyed by content hash |
| Certificate pinning | Pinned clients fail outright behind a MITM proxy | Maintain the passthrough list; ship a sensible default list |
| Content Security Policy | Pages with strict CSP block any injected script (Phase 4 only) | Strip or relax `Content-Security-Policy` headers on an allowlist of hosts, and document the security trade-off |
| HTTP/3 and WebSockets | QUIC bypasses the proxy; WebSocket image payloads are not inspected | Block UDP 443; WebSockets remain out of scope |
| Canvas and JS-built images | Never visible as a decodable response | Phase 4 DOM observer, or accept the gap |
| Memory | Full-body buffering of large images and GIFs | `stream_large_bodies` ceiling for binary types; frame-by-frame GIF processing rather than loading all frames at once |
| Model availability | NudeNet and OpenNSFW2 weights are downloaded at first run from GitHub releases | Bake weights into the image at build time so the container runs offline |
| Privacy of the log | The detection log records every URL that triggered a detection | Keep it local, rotate it, and make logging of URLs optional |

## Implementation plan

Work is sequenced so a usable proxy exists after Phase 1 and every later phase widens coverage or cuts latency. No dates are set yet; each phase ends at a demo gate rather than a calendar date.

&#91;embedded content: implementation roadmap · 4 phases, 3 gates\]

Phase 4 is dashed because it should only be built if Phase 3 tuning shows specific sites are slipping through; it is the only phase that touches page content beyond images.

**Phase 1 task list**

- [ ] Dockerfile on `python:3.11-slim` with mitmproxy, nudenet, opencv-python-headless, onnxruntime; model weights baked in
- [ ] `censor.py` addon: `response()` hook, Content-Type routing, passthrough check, size floor
- [ ] `censor_bytes()` with NudeNet detection, `FEMALE_` label filter, OpenCV blur
- [ ] `config.yaml` loader for thresholds, blur style, passthrough regexes
- [ ] JSONL detection logger
- [ ] README covering CA install per OS and proxy settings
- [ ] Test fixtures: a small set of labelled images and a script that asserts the expected boxes are blurred

## Open questions

These decisions change the Phase 1 design and should be settled before coding starts.

- [ ] **Host OS**: Linux, macOS or Windows? This decides whether transparent mode is practical and how the CA is installed.
- [ ] **CPU or GPU**: Is an NVIDIA GPU available to the container? CPU-only is fine for still images but GIF-heavy sites will feel slow.
- [ ] **Censor style**: Gaussian blur, pixelation or solid box? Blur reads most naturally; solid boxes are cheapest.
- [ ] **Fail open or fail closed**: If the model throws or times out, should the image pass through unmodified, or be delivered with a full-frame blur applied?
- [ ] **Scope of "explicit"**: Exposed anatomy only (NudeNet `_EXPOSED` labels), or also covered-but-suggestive content, which would require the InsightFace fallback in Phase 3?
- [ ] **Uncertain-gender handling**: When NudeNet returns no sex-specific label but the pre-filter score is high, censor anyway or pass through?
- [ ] **Log contents**: Record full URLs, hostnames only, or no URLs at all?
- [ ] **Browser set**: Which browsers must work? Firefox needs a separate CA install; Chrome may need HTTP/3 disabled by policy rather than firewall.
