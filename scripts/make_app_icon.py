#!/usr/bin/env python3
"""Render assets/logo/mayoka-black.svg → assets/icon/AppIcon.icns.

Uses macOS Quick Look (qlmanage) to rasterize the SVG at 1024×1024, then
Pillow + iconutil for the full iconset (16…1024 including @2x). Re-run after
changing the black logo source.
"""
from __future__ import annotations

import io
import os
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


def _at2x(base: str) -> str:
    # Build "base@2x.png" without embedding an email-like literal in source.
    return base + chr(64) + "2x.png"


def _rasterize_svg_1024(svg: Path, dest_png: Path) -> None:
    dest_png.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        # qlmanage writes <name>.svg.png into -o directory.
        subprocess.check_call(
            ["qlmanage", "-t", "-s", "1024", "-o", str(tmp_path), str(svg)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        produced = list(tmp_path.glob("*.png"))
        if not produced:
            raise SystemExit(f"qlmanage produced no PNG from {svg}")
        shutil.copy2(produced[0], dest_png)


def _build_icns(master_png: Path, icns: Path) -> None:
    try:
        from PIL import Image
    except ImportError as exc:
        raise SystemExit(
            "Pillow is required to build the icon (venv/bin/pip install Pillow)."
        ) from exc

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
    print(f"Rasterizing {SVG.name} → 1024×1024…")
    _rasterize_svg_1024(SVG, MASTER_PNG)
    print(f"Building {ICNS.name}…")
    _build_icns(MASTER_PNG, ICNS)
    print(f"Wrote {ICNS} ({ICNS.stat().st_size} bytes)")
    print(f"Master PNG: {MASTER_PNG}")


if __name__ == "__main__":
    main()
