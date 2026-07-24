#!/usr/bin/env python3
"""Render assets/logo/mayoka-black.svg → assets/icon/AppIcon.icns.

Built from Apple's official App Icon production template, not an approximated
squircle formula.

## Source (verify yourself)

- Apple Design Resources → iOS & iPadOS 27 → **App Icon Template**
  https://developer.apple.com/design/resources/
- Direct download used for measurements:
  https://devimages-cdn.apple.com/design/resources/download/iOS-27-Icon-Templates-Photoshop-Illustrator.dmg
- Official Figma (same grid):
  https://www.figma.com/community/file/1645923469870515372/app-icon-template-ios-ipados-and-watchos-27
- HIG (iOS / iPadOS / **macOS** share square production layers; system masks):
  https://developer.apple.com/design/human-interface-guidelines/app-icons

The DMG ships `App Icon Template.psd` (1024×1024). Photoshop guide resource
0x0408 in that file defines the official grid (measured Jun 2026):

    Canvas:           1024 × 1024 px
    Grid guides (H+V): 128, 256, 384, 512, 640, 768, 896
    Cell size:        128 px  (1024 / 8)
    Content / safe:   128 … 896  →  768 × 768  (75% of production canvas)
    Outer grid margin: 128 px each side (12.5%)

Platform note (Icon Composer `icon.json` in the same DMG):
    "supported-platforms": { "squares": "shared" }
so this production grid is the shared square template for iOS / iPadOS / macOS.
(macOS 27 on the design-resources page only lists a UI Kit; app-icon production
grid lives in the App Icon Template above — confirmed by HIG + Icon Composer.)

## Corner shape

The DMG's Icon Composer marketing export
`Demo Project-iOS-Default-1024@1x.png` is Apple's own full-bleed continuous-
corner mask of that template. We store only its **alpha channel** as
`assets/icon/apple_official_icon_shape_mask.png` (geometry, no demo art).

A superellipse (n≈5) is NOT used — that was the previous approximation.

## macOS Dock presentation (.icns)

HIG production layers are full-bleed squares. Classic CFBundleIconFile `.icns`
files (Finder, Safari, Messages, third-party apps) do **not** fill the canvas:
measured content width is consistently ~82.03125% (840/1024), with transparent
margin so Dock neighbours match in visual weight.

    SHAPE_SIZE = 840   # 0.8203125 × 1024 — matches system .icns at 512px

Pipeline:
  1. Rasterize mayoka-black.svg
  2. Full-bleed composite on 1024: white plate + logo in official 768 safe box
  3. Apply Apple's official shape mask (full-bleed)
  4. Scale that masked icon down to 840 and centre on a transparent 1024 canvas
  5. iconutil → full .icns size set
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
# Pure alpha geometry from Apple's official Icon Composer export (see module doc).
OFFICIAL_SHAPE_MASK = OUT_DIR / "apple_official_icon_shape_mask.png"

# --- Official App Icon Template.psd measurements (1024 production canvas) ---
CANVAS = 1024
# Guides: 128, 256, 384, 512, 640, 768, 896 — content box is the inner 6×6 cells.
GRID_CELL = 128
CONTENT_ORIGIN = GRID_CELL          # 128
CONTENT_SIZE = CANVAS - 2 * GRID_CELL  # 768  (128 … 896)

# --- macOS Dock .icns presentation (measured against system apps) ---
# Finder / Safari / Messages / Notes / … at 512: content_frac == 0.8203125.
SHAPE_SIZE = 840  # int(1024 * 0.8203125)


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


def _load_official_shape_mask(size: int = CANVAS):
    """Apple's official continuous-corner shape as an L mask (full-bleed)."""
    from PIL import Image

    if not OFFICIAL_SHAPE_MASK.is_file():
        raise SystemExit(
            f"Missing official shape mask: {OFFICIAL_SHAPE_MASK}\n"
            "Regenerate it from Apple's App Icon Template DMG (see module docstring)."
        )
    mask = Image.open(OFFICIAL_SHAPE_MASK).convert("L")
    if mask.size != (size, size):
        mask = mask.resize((size, size), Image.Resampling.LANCZOS)
    return mask


def _prepare_logo_mark(logo_png: Path) -> "Image.Image":
    """Raster → transparent-bg mark, cropped to ink (qlmanage adds white pad)."""
    from PIL import Image

    logo = Image.open(logo_png).convert("RGBA")
    px = logo.load()
    w, h = logo.size
    # qlmanage paints a white plate under the SVG; punch that out and crop to ink.
    minx, miny, maxx, maxy = w, h, 0, 0
    found = False
    for y in range(h):
        for x in range(w):
            r, g, b, a = px[x, y]
            if a < 8:
                continue
            if r > 240 and g > 240 and b > 240:
                px[x, y] = (255, 255, 255, 0)
                continue
            found = True
            if x < minx:
                minx = x
            if y < miny:
                miny = y
            if x > maxx:
                maxx = x
            if y > maxy:
                maxy = y
    if not found:
        raise SystemExit(f"No ink found in rasterized logo: {logo_png}")
    return logo.crop((minx, miny, maxx + 1, maxy + 1))


def _compose_mac_icon(logo_png: Path, out_png: Path) -> None:
    """White plate + logo on official grid, Apple shape, Dock-sized presentation."""
    from PIL import Image

    # 1) Full-bleed production composite (1024), per official template.
    production = Image.new("RGBA", (CANVAS, CANVAS), (255, 255, 255, 255))

    logo = _prepare_logo_mark(logo_png)
    # Fit mark inside the official content / safe box (768×768 at origin 128).
    lw, lh = logo.size
    scale = min(CONTENT_SIZE / float(lw), CONTENT_SIZE / float(lh))
    nw = max(1, int(round(lw * scale)))
    nh = max(1, int(round(lh * scale)))
    logo = logo.resize((nw, nh), Image.Resampling.LANCZOS)
    # Centre within the content box (which is itself centred on the canvas).
    content_cx = CONTENT_ORIGIN + CONTENT_SIZE / 2.0
    content_cy = CONTENT_ORIGIN + CONTENT_SIZE / 2.0
    ox = int(round(content_cx - nw / 2.0))
    oy = int(round(content_cy - nh / 2.0))
    production.paste(logo, (ox, oy), logo)

    # 2) Apply Apple's official full-bleed continuous-corner mask.
    shape = _load_official_shape_mask(CANVAS)
    production.putalpha(shape)

    # 3) Scale to Dock presentation size (840) and centre on transparent canvas.
    #    Matches system .icns content weight next to Finder / Safari / etc.
    shaped = production.resize((SHAPE_SIZE, SHAPE_SIZE), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    paste_at = (CANVAS - SHAPE_SIZE) // 2  # 92
    canvas.paste(shaped, (paste_at, paste_at), shaped)

    out_png.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_png, format="PNG")


def _build_icns(master_png: Path, icns: Path) -> None:
    from PIL import Image

    img = Image.open(master_png).convert("RGBA")
    if img.size != (CANVAS, CANVAS):
        img = img.resize((CANVAS, CANVAS), Image.Resampling.LANCZOS)

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
        print(
            "Compositing from Apple official App Icon Template: "
            f"canvas={CANVAS}, content_safe={CONTENT_SIZE}@{CONTENT_ORIGIN}, "
            f"shape_mask=official, dock_shape={SHAPE_SIZE}…"
        )
        _compose_mac_icon(raw, MASTER_PNG)

    print(f"Building {ICNS.name}…")
    _build_icns(MASTER_PNG, ICNS)
    print(f"Wrote {ICNS} ({ICNS.stat().st_size} bytes)")
    print(f"Master PNG: {MASTER_PNG}")


if __name__ == "__main__":
    main()
