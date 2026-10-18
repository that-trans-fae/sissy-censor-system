# Design decisions

Answers to the open questions in the project handoff, settled Oct 18, 2026.

| Question | Decision |
| --- | --- |
| Host OS | Linux (Ubuntu 26.04). Explicit proxy first; transparent mode optional. |
| CPU or GPU | CPU only. The host GPU is an AMD RX 580 (no CUDA, no current ROCm support). |
| Censor style | Gaussian blur by default; `censor.style` is configurable (`gaussian`, `pixelate`, `solid`). |
| Fail open or closed | Fail closed: if detection errors or times out, blur the whole image and deliver it. |
| Scope of "explicit" | Exposed anatomy only (`*_EXPOSED` labels). No InsightFace fallback in v1. |
| Female-only selection | Censor an explicit allowlist of labels rather than matching a `FEMALE_` prefix. Unsexed labels (`BUTTOCKS_EXPOSED`, `ANUS_EXPOSED`) count as female when they overlap or sit near a female detection (`FEMALE_*` or `FACE_FEMALE`). If an image has no female detections, those unsexed boxes are still censored, but nothing else is (never the whole image). |
| Context thresholds | Female labels use 0.5 by default; 0.35 when the box touches another confident female box (the missed second breast in `made-to-be-thrown-around`); 0.65 when nothing else in the image is female but male context is present (the male chest misread as female in `rub-an-irish…`). Both configurable. Tuned on 15 samples, so expect to revisit. |
| Model | 320n for now, for speed (~0.1 s vs ~0.6 s per image). 640m stays baked into the image as a config switch. On the 15 samples 640m gets every image right; 320n misses `mixed/daddy-said` (reads exposed breasts as covered) and `mixed/what-kinda-cage` (its one breast score overlaps the `rub-an-irish` false positive). Both are recorded as known failures in the 320n fixture. |
| Unsexed attribution tweaks | A female detection overlapping an unsexed box counts from 0.25 and overrides the male exemption (`mixed/cum-covered-back`: a woman's buttocks next to a male detection). Context detections count from 0.35, not 0.4 (fixes `uncensor/completely-helpless` under 320n; no change under 640m). |
| Pre-filter | OpenNSFW2 dropped (it pulls in TensorFlow). NudeNet runs on every image above the size floor; repeat images are served from a content-hash cache. |
| Log contents | Hostnames by default. Full URLs are logged only at the more verbose level. Configurable. |
| Browsers | Firefox and Chromium (both snaps). Chromium trusts the CA through the `CACertificates` policy in `/var/snap/chromium/current/policies/managed/`; its updater hosts are passed through. Verified with a test profile on live HTTPS (Reddit images censored in 76-294 ms with 320n). Still to do: system-wide proxy via the PAC file, and Firefox. |
| Fail-open when container is down | Use a PAC file (`PROXY localhost:8080; DIRECT`) rather than relying on a health check. |
| Test fixtures | Run NudeNet on `docs/samples/`, draw the boxes, and have them confirmed by hand before freezing them as expected results. |
| Layout | Code in `src/` next to `docs/`; git repo at the project root. |
