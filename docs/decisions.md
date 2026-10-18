# Design decisions

Answers to the open questions in the project handoff, settled Oct 18, 2026.

| Question | Decision |
| --- | --- |
| Host OS | Linux (Ubuntu 26.04). Explicit proxy first; transparent mode optional. |
| CPU or GPU | CPU only. The host GPU is an AMD RX 580 (no CUDA, no current ROCm support). |
| Censor style | Gaussian blur by default; `censor.style` is configurable (`gaussian`, `pixelate`, `solid`). |
| Fail open or closed | Fail closed: if detection errors or times out, blur the whole image and deliver it. |
| Scope of "explicit" | Exposed anatomy only (`*_EXPOSED` labels). No InsightFace fallback in v1. |
| Female-only selection | Censor an explicit allowlist of labels (`FEMALE_BREAST_EXPOSED`, `FEMALE_GENITALIA_EXPOSED`) rather than matching a `FEMALE_` prefix. Unsexed labels (`BUTTOCKS_EXPOSED`, `ANUS_EXPOSED`) are censored unless they sit near male context and not near female context (`FEMALE_*`, `FACE_FEMALE`); with no attribution either way they are censored. Never the whole image. |
| Context thresholds | Female labels use 0.5 by default; 0.35 when the box touches another confident female box (the missed second breast in `made-to-be-thrown-around`); 0.65 when nothing else in the image is female but male context is present (the male chest misread as female in `rub-an-irish…`). Both configurable. Tuned on 15 samples, so expect to revisit. |
| Model | 320n for now, for speed (~0.25 s vs ~2 s per image with tiling). 640m stays baked into the image as a config switch. On the 15 samples 640m gets every image right; 320n misses `mixed/daddy-said` (reads exposed breasts as covered) and `mixed/what-kinda-cage` (its one breast score overlaps the `rub-an-irish` false positive). Both are recorded as known failures in the 320n fixture. |
| Unsexed attribution tweaks | A female detection overlapping an unsexed box counts from 0.25 and overrides the male exemption (`mixed/cum-covered-back`: a woman's buttocks next to a male detection). Context detections count from 0.35, not 0.4 (fixes `uncensor/completely-helpless` under 320n; no change under 640m). |
| Data URIs | One bytes-level regex over HTML, CSS and JSON bodies instead of selectolax parsing (dropped from the image): it covers `src`, `srcset`, inline styles, CSS `url()` and JSON strings, including JSON's `\/` escaping. Each image gets the per-image timeout; failures fail that image closed. |
| Recall over precision | False positives are preferred to misses, but breasts at 0.3 over-triggered, so they sit at the midpoint of what was tested. Default threshold 0.3 (genitalia, buttocks); `FEMALE_BREAST_EXPOSED` 0.4; second-of-a-pair 0.3; lone female box with male context 0.625. Tall or wide images are also scanned as overlapping square tiles (`model.tiling`), which recovers small regions lost when a 1080x1900 phone photo is shrunk to the model input. On 44 samples: 320n 36 right (6 misses, 2 male false positives) at ~0.25 s/image median; 640m 40 right (4 misses, no false positives) at ~2 s/image. Long animations scan at most 48 frames; the image size limit is 64 MB so large GIFs are not streamed through unscanned. |
| Pre-filter | OpenNSFW2 dropped (it pulls in TensorFlow). NudeNet runs on every image above the size floor; repeat images are served from a content-hash cache. |
| Log contents | Hostnames by default. Full URLs are logged only at the more verbose level. Configurable. |
| Browsers | Firefox and Chromium (both snaps). Chromium trusts the CA through the `CACertificates` policy in `/var/snap/chromium/current/policies/managed/`; its updater hosts are passed through. Verified with a test profile on live HTTPS (Reddit images censored in 76-294 ms with 320n). Still to do: system-wide proxy via the PAC file, and Firefox. |
| Fail-open when container is down | Use a PAC file (`PROXY localhost:8080; DIRECT`) rather than relying on a health check. |
| Test fixtures | Run `tools/draw_boxes.py` on `docs/samples/`, have the boxes confirmed by eye, then freeze them per model (`src/tests/fixtures/expected-<model>.json`). Folder names are ground truth: `censor/` and `mixed/` must be censored, `uncensor/` clean. A model's known failures stay in its fixture as strict xfails with the correct result. Samples are not in git. |
| Layout | Code in `src/` next to `docs/`; git repo at the project root. |
| Runtime | Python 3.13, not the handoff's 3.11: mitmproxy 12 needs 3.12+. The 640m weights are fetched at build time through the GitHub API asset endpoint (the release URL redirects scripts to a login page) and checksum-pinned. onnxruntime telemetry is disabled so nothing leaves the machine. |
| Docker | Docker Desktop (a VM). Ports bind to 127.0.0.1 only. Transparent mode is impractical here, so the proxy is explicit only. |
