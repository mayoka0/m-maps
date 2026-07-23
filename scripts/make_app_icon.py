#!/usr/bin/env python3
"""Render assets/logo/mayoka-black.svg → assets/icon/AppIcon.icns.

macOS Dock icons are NOT auto-masked like iOS:
  - Artwork needs real padding (~10% margin → content ~80% of canvas).
  - The canvas should already be a continuous rounded square (squircle),
    not a hard-edged full-bleed square.

Pipeline:
  1. Rasterize the black mark (qlmanage).
  2. Composite onto a white squircle with padding.
  3. iconutil → full .icns size set (16…1024 + @2x).
"""
from __future__ import annotations

import io
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
SVG = PROJECT / "assets" / "logo" / "mayoka-black.svg"
OUT_DIR = PROJECT / "assets" / "icon"
ICNS = OUT_DIR / "AppIcon.icns"
MASTER_PNG = OUT_DIR / "AppIcon-1024.png"

# Content scale: logo occupies this fraction of the canvas (rest is margin).
LOGO_SCALE = 0.78
# Superellipse exponent for Apple-like continuous corners (squircle).
# n≈5 is closer to Apple's icon mask than a simple rounded rect.
SQUIRCLE_N = 5.0
# Soft edge AA for the mask.
MASK_BLUR = 1.2


def _at2x(base: str) -> str:
    return base + chr(64) + "2x.png"


def _rasterize_svg(svg: Path, dest_png: Path, pixel_size: int = 1024) -> None:
    dest_png.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        subprocess.check_call(
            ["qlmanage", "-t", "-s", str(pixel_size), "-o", str(tmp_path), str(svg)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        produced = list(tmp_path.glob("*.png"))
        if not produced:
            raise SystemExit(f"qlmanage produced no PNG from {svg}")
        shutil.copy2(produced[0], dest_png)


def _squircle_mask(size: int, n: float = SQUIRCLE_N):
    """Luminance mask: white inside continuous corner shape, black outside."""
    from PIL import Image, ImageFilter

    # Build at 2× then downscale for cleaner edges.
    big = size * 2
    mask = Image.new("L", (big, big), 0)
    px = mask.load()
    cx = cy = (big - 1) / 2.0
    r = big / 2.0
    # Slight inset so the mask sits inside the canvas (avoids clipped corners).
    r *= 0.995
    inv_n = 1.0 / n
    for y in range(big):
        ny = abs((y - cy) / r)
        if ny > 1.0:
            continue
        # |x/r|^n + |y/r|^n <= 1  →  |x/r| <= (1 - |y/r|^n)^(1/n)
        limit = (1.0 - ny ** n) ** inv_n
        half = int(limit * r)
        x0 = max(0, int(cx - half))
        x1 = min(big - 1, int(cx + half))
        for x in range(x0, x1 + 1):
            nx = abs((x - cx) / r)
            if nx ** n + ny ** n <= 1.0:
                px[x, y] = 255
    mask = mask.resize((size, size), Image.Resampling.LANCZOS)
    if MASK_BLUR > 0:
        mask = mask.filter(ImageFilter.GaussianBlur(radius=MASK_BLUR))
    return mask


def _compose_mac_icon(logo_png: Path, out_png: Path, size: int = 1024) -> None:
    """White squircle + padded black mark → final 1024 master for iconutil."""
    from PIL import Image

    logo = Image.open(logo_png).convert("RGBA")
    # Fit logo into LOGO_SCALE of the canvas, preserving aspect.
    max_side = int(size * LOGO_SCALE)
    lw, lh = logo.size
    scale = min(max_side / float(lw), max_side / float(lh))
    nw, nh = max(1, int(round(lw * scale))), max(1, int(round(lh * scale)))
    logo = logo.resize((nw, nh), Image.Resampling.LANCZOS)

    # White fill, then paste logo centered (keep alpha of mark).
    canvas = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    ox = (size - nw) // 2
    oy = (size - nh) // 2
    canvas.paste(logo, (ox, oy), logo)

    mask = _squircle_mask(size)
    # Apply squircle: outside corners become fully transparent.
    canvas.putalpha(mask)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_png, format="PNG")


def _build_icns(master_png: Path, icns: Path) -> None:
    from PIL import Image

    img = Image.open(master_png).convert("RGBA")
    if img.size != (1024, 1024):
        img = img.resize((1024, 1024), Image.Resampling.LANCZOS)

    entries = [
        (16, "icon_16x16.png"),
        (32, _at2x("icon_16x16")),
        (32, "icon_32x32.png"),
        (64, _at2x("icon_32x32")),
        (128, "icon_128x128.png"),
        (256, _at2x("icon_128x128")),
        (256, "icon_256x256.png"),
        (512, _at2x("icon_256x256")),
        (512, "icon_512x512.png"),
        (1024, _at2x("icon_512x512")),
    ]

    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "AppIcon.iconset"
        iconset.mkdir()
        for size, name in entries:
            buf = io.BytesIO()
            img.resize((size, size), Image.Resampling.LANCZOS).save(buf, format="PNG")
            (iconset / name).write_bytes(buf.getvalue())
        if len(list(iconset.iterdir())) != 10:
            raise SystemExit("iconset incomplete — expected 10 PNGs")
        icns.parent.mkdir(parents=True, exist_ok=True)
        subprocess.check_call(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(icns)]
        )


def main() -> None:
    if sys.platform != "darwin":
        raise SystemExit("make_app_icon.py is macOS-only (qlmanage + iconutil).")
    if not SVG.is_file():
        raise SystemExit(f"Missing logo source: {SVG}")

    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "logo-raw.png"
        print(f"Rasterizing {SVG.name}…")
        _rasterize_svg(SVG, raw, pixel_size=1024)
        print("Compositing padded squircle icon (white plate + ~78% mark)…")
        _compose_mac_icon(raw, MASTER_PNG, size=1024)

    print(f"Building {ICNS.name}…")
    _build_icns(MASTER_PNG, ICNS)
    print(f"Wrote {ICNS} ({ICNS.stat().st_size} bytes)")
    print(f"Master PNG: {MASTER_PNG}")


if __name__ == "__main__":
    main()
