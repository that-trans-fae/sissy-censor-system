"""Draw detection boxes for a folder of sample images, for manual review.

    python tools/draw_boxes.py SAMPLES_DIR OUT_DIR [--config FILE]

For each image writes OUT_DIR/<subdir>/<name>.boxes.png (every detection,
red = censored, grey = passed, with label/score/reason) and <name>.censored.png
(the proxy's actual output); animations show the sampled frame with the most
censored boxes, plus OUT_DIR/index.html to view them side by side
and OUT_DIR/detections.json with every decision, which becomes the test fixture
once confirmed.
"""

from __future__ import annotations

import argparse
import html
import io
import json
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from censor_proxy.config import load_config  # noqa: E402
from censor_proxy.imaging import Censor  # noqa: E402

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
RED, GREY = (230, 40, 40), (150, 150, 150)


def frame_rgb(data: bytes, index: int) -> np.ndarray:
    im = Image.open(io.BytesIO(data))
    im.seek(index)
    return np.array(im.convert("RGB"))


def annotate(rgb: np.ndarray, decisions) -> np.ndarray:
    out = rgb.copy()
    scale = max(0.5, max(out.shape[:2]) / 1400)
    thick = max(2, round(scale * 2))
    # Passed boxes first so censored ones draw on top.
    for d in sorted(decisions, key=lambda d: d.censor):
        b, color = d.detection.box, RED if d.censor else GREY
        cv2.rectangle(out, (b.x, b.y), (b.x + b.w, b.y + b.h), color, thick)
        text = f"{d.detection.label} {d.detection.score:.2f}"
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
        ty = b.y + th + 4 if b.y < th + 6 else b.y - 4
        cv2.rectangle(out, (b.x, ty - th - 4), (b.x + tw + 4, ty + 3), color, -1)
        cv2.putText(out, text, (b.x + 2, ty), cv2.FONT_HERSHEY_SIMPLEX, scale,
                    (255, 255, 255), 1, cv2.LINE_AA)
    return out


def save_png(rgb: np.ndarray, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgb).save(path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("samples", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--config", type=Path)
    args = ap.parse_args()

    cfg = load_config(args.config)
    censor = Censor(cfg)
    records, rows = [], []

    for path in sorted(p for p in args.samples.rglob("*") if p.suffix.lower() in IMAGE_SUFFIXES):
        rel = path.relative_to(args.samples)
        data = path.read_bytes()
        result = censor.censor_bytes(data)
        # For animations, show the sampled frame with the most censored boxes.
        frame, decisions = max(result.frame_decisions or [(0, [])],
                               key=lambda fd: (sum(d.censor for d in fd[1]), -fd[0]))

        stem = rel.with_suffix("")
        boxes_png = args.out / f"{stem}.boxes.png"
        censored_png = args.out / f"{stem}.censored.png"
        save_png(annotate(frame_rgb(data, frame), decisions), boxes_png)
        save_png(frame_rgb(result.data, frame), censored_png)

        records.append({
            "file": str(rel),
            "action": result.action,
            **({"frame": frame, "frames": result.frames} if result.frames > 1 else {}),
            "elapsed_ms": round(result.elapsed_ms),
            "detections": [{**d.detection.as_dict(), "censor": d.censor, "why": d.reason}
                           for d in decisions],
        })
        table = "".join(
            f"<tr class={'c' if d.censor else 'p'}><td>{'censor' if d.censor else 'pass'}</td>"
            f"<td>{d.detection.label}</td><td>{d.detection.score:.2f}</td>"
            f"<td>{html.escape(d.reason)}</td></tr>"
            for d in sorted(decisions, key=lambda d: (not d.censor, -d.detection.score)))
        rows.append(
            f"<section><h2>{html.escape(str(rel))} <small>{result.action}, "
            f"{result.elapsed_ms:.0f} ms"
            f"{f', frame {frame} of {result.frames}' if result.frames > 1 else ''}</small></h2>"
            f"<div class=pair><img src='{html.escape(str(boxes_png.relative_to(args.out)))}'>"
            f"<img src='{html.escape(str(censored_png.relative_to(args.out)))}'></div>"
            f"<table>{table or '<tr><td>no detections</td></tr>'}</table></section>")
        print(f"{result.action:9} {result.elapsed_ms:6.0f} ms  {rel}")

    (args.out / "detections.json").write_text(json.dumps(records, indent=2) + "\n")
    (args.out / "index.html").write_text(f"""<!doctype html><meta charset=utf-8>
<title>Box review</title>
<style>
body{{font:14px system-ui;margin:16px;background:#111;color:#ddd}}
section{{margin-bottom:40px}} h2 small{{color:#999;font-weight:normal}}
.pair{{display:flex;gap:8px}} .pair img{{max-width:49%;max-height:80vh;object-fit:contain}}
table{{border-collapse:collapse;margin-top:8px}} td{{padding:2px 10px;border-bottom:1px solid #333}}
tr.c td:first-child{{color:#f66;font-weight:bold}} tr.p{{color:#999}}
</style>
<h1>Box review</h1>
<p>Left: every detection (red = censored, grey = passed). Right: proxy output.
Style: {cfg.censor.style}; threshold {cfg.detection.threshold}; model {cfg.model.path or '320n (bundled)'}.</p>
{''.join(rows)}
""")
    print(f"\nwrote {args.out / 'index.html'}")


if __name__ == "__main__":
    main()
