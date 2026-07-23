#!/usr/bin/env python3
"""Build a polished macOS .dmg installer for M Maps.

Layout (standard drag-to-install):
  - ``M Maps.app`` on the left
  - shortcut to ``/Applications`` on the right
  - branded background with a subtle arrow cue
  - compact Finder window (~660×420), not a huge empty frame

Requires a prior ``build_app.py`` run (``dist/M Maps.app``). Output:
``dist/M Maps.dmg``. Uses only macOS built-ins (hdiutil + AppleScript).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DIST = PROJECT_ROOT / "dist"
APP_NAME = "M Maps"
APP_BUNDLE = DIST / f"{APP_NAME}.app"
DMG_PATH = DIST / f"{APP_NAME}.dmg"
VOLUME_NAME = "M Maps"
ICON_ICNS = PROJECT_ROOT / "assets" / "icon" / "AppIcon.icns"
LOGO_SVG = PROJECT_ROOT / "assets" / "logo" / "mayoka-black.svg"
MASTER_ICON_PNG = PROJECT_ROOT / "assets" / "icon" / "AppIcon-1024.png"

# Finder window geometry — right/bottom = left+width / top+height.
WINDOW_LEFT = 200
WINDOW_TOP = 140
WINDOW_WIDTH = 660
WINDOW_HEIGHT = 420
ICON_SIZE = 96
# Icon positions in content view coordinates (y grows downward in Finder AS).
APP_XY = (170, 185)
APPS_XY = (490, 185)


def _run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, **kwargs)


def _detach_all(volume_name: str) -> None:
    mount = Path("/Volumes") / volume_name
    for _ in range(5):
        if not mount.exists():
            return
        subprocess.run(
            ["hdiutil", "detach", str(mount), "-force"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.4)


def _attach_rw(dmg: Path) -> Path:
    out = subprocess.check_output(
        ["hdiutil", "attach", "-readwrite", "-noverify", "-noautoopen", str(dmg)],
        text=True,
    )
    for line in out.splitlines():
        if "/Volumes/" in line:
            m = re.search(r"(/Volumes/.+)$", line)
            if m:
                return Path(m.group(1).strip())
            return Path(line.split("\t")[-1].strip())
    raise RuntimeError(f"Could not find mount point in hdiutil output:\n{out}")


def _make_background_png(path: Path) -> None:
    """Simple branded installer background (dark panel + soft arrow + mark)."""
    from PIL import Image, ImageDraw

    w, h = WINDOW_WIDTH, WINDOW_HEIGHT
    # Match the app UI dark panel.
    bg = (11, 14, 20, 255)
    accent = (59, 130, 246, 220)
    muted = (144, 160, 179, 180)
    img = Image.new("RGBA", (w, h), bg)
    draw = ImageDraw.Draw(img)

    # Soft top bar
    draw.rectangle([0, 0, w, 48], fill=(20, 26, 35, 255))
    draw.text((24, 16), "Install M Maps — drag to Applications", fill=(230, 237, 243, 255))

    # Subtle arrow from app → Applications
    y = APP_XY[1] + 8
    x0 = APP_XY[0] + ICON_SIZE // 2 + 36
    x1 = APPS_XY[0] - ICON_SIZE // 2 - 36
    mid = (x0 + x1) // 2
    draw.line([(x0, y), (x1 - 12, y)], fill=accent, width=3)
    # Arrow head
    draw.polygon(
        [(x1, y), (x1 - 14, y - 8), (x1 - 14, y + 8)],
        fill=accent,
    )
    draw.text((mid - 18, y + 14), "drag", fill=muted)

    # Optional small logo watermark bottom-right
    logo_src = MASTER_ICON_PNG if MASTER_ICON_PNG.is_file() else None
    if logo_src is not None:
        try:
            mark = Image.open(logo_src).convert("RGBA")
            mark.thumbnail((72, 72), Image.Resampling.LANCZOS)
            # Fade
            alpha = mark.split()[-1].point(lambda p: int(p * 0.25))
            mark.putalpha(alpha)
            img.paste(mark, (w - 88, h - 88), mark)
        except Exception:
            pass

    path.parent.mkdir(parents=True, exist_ok=True)
    # Finder wants a plain RGB background picture.
    img.convert("RGB").save(path, format="PNG")


def _set_finder_layout(volume: Path, bg_name: str = "background.png") -> None:
    """Size the window, set background, position icons."""
    # Use HFS path for background file inside the volume.
    right = WINDOW_LEFT + WINDOW_WIDTH
    bottom = WINDOW_TOP + WINDOW_HEIGHT
    script = f'''
tell application "Finder"
    tell disk "{VOLUME_NAME}"
        open
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set the bounds of container window to {{{WINDOW_LEFT}, {WINDOW_TOP}, {right}, {bottom}}}
        set viewOptions to the icon view options of container window
        set arrangement of viewOptions to not arranged
        set icon size of viewOptions to {ICON_SIZE}
        try
            set background picture of viewOptions to file ".background:{bg_name}"
        end try
        set position of item "{APP_NAME}.app" of container window to {{{APP_XY[0]}, {APP_XY[1]}}}
        set position of item "Applications" of container window to {{{APPS_XY[0]}, {APPS_XY[1]}}}
        update without registering applications
        delay 0.8
        close
        open
        set the bounds of container window to {{{WINDOW_LEFT}, {WINDOW_TOP}, {right}, {bottom}}}
        delay 0.6
        close
    end tell
end tell
'''
    _run(["osascript", "-e", script])


def build() -> Path:
    if sys.platform != "darwin":
        raise SystemExit("Building the .dmg is macOS-only.")
    if not APP_BUNDLE.is_dir():
        raise SystemExit(
            f"Missing {APP_BUNDLE}. Run: venv/bin/python3 build_app.py first."
        )
    if not ICON_ICNS.is_file():
        raise SystemExit(f"Missing {ICON_ICNS}")

    DIST.mkdir(parents=True, exist_ok=True)
    _detach_all(VOLUME_NAME)

    with tempfile.TemporaryDirectory(prefix="mmaps-dmg-") as tmp:
        stage = Path(tmp) / "stage"
        stage.mkdir()
        shutil.copytree(APP_BUNDLE, stage / f"{APP_NAME}.app", symlinks=True)
        os.symlink("/Applications", stage / "Applications")
        shutil.copy2(ICON_ICNS, stage / ".VolumeIcon.icns")

        # Hidden background folder (Finder looks up .background:background.png).
        bg_dir = stage / ".background"
        bg_dir.mkdir()
        _make_background_png(bg_dir / "background.png")

        rw_dmg = Path(tmp) / "rw.dmg"
        if DMG_PATH.exists():
            DMG_PATH.unlink()

        _run(
            [
                "hdiutil",
                "create",
                "-volname",
                VOLUME_NAME,
                "-srcfolder",
                str(stage),
                "-ov",
                "-format",
                "UDRW",
                "-fs",
                "HFS+",
                str(rw_dmg),
            ],
            stdout=subprocess.DEVNULL,
        )

        volume = _attach_rw(rw_dmg)
        try:
            # Ensure hidden items + background survive attach (srcfolder can drop dots).
            vol_bg = volume / ".background"
            vol_bg.mkdir(exist_ok=True)
            if not (vol_bg / "background.png").is_file():
                _make_background_png(vol_bg / "background.png")
            # Hide the background folder from icon view.
            try:
                subprocess.run(
                    ["SetFile", "-a", "V", str(vol_bg)],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except FileNotFoundError:
                pass

            _set_finder_layout(volume)

            vol_icon = volume / ".VolumeIcon.icns"
            shutil.copy2(ICON_ICNS, vol_icon)
            try:
                subprocess.run(
                    ["SetFile", "-a", "C", str(volume)],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except (FileNotFoundError, subprocess.CalledProcessError):
                pass
            if not vol_icon.is_file() or vol_icon.stat().st_size < 1000:
                raise RuntimeError("Failed to write .VolumeIcon.icns on the DMG volume")

            subprocess.run(["sync"], check=False)
            time.sleep(1.0)
        finally:
            for _ in range(6):
                r = subprocess.run(
                    ["hdiutil", "detach", str(volume)],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if r.returncode == 0:
                    break
                time.sleep(0.5)
            else:
                subprocess.run(
                    ["hdiutil", "detach", str(volume), "-force"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            time.sleep(0.5)

        _run(
            [
                "hdiutil",
                "convert",
                str(rw_dmg),
                "-format",
                "UDZO",
                "-imagekey",
                "zlib-level=9",
                "-o",
                str(DMG_PATH),
            ],
            stdout=subprocess.DEVNULL,
        )

    print(f"Built: {DMG_PATH}")
    print(f"Size:  {DMG_PATH.stat().st_size // 1024} KB")
    print(f"Window: {WINDOW_WIDTH}×{WINDOW_HEIGHT}, icons {ICON_SIZE}px")
    print(f"Open:  open {DMG_PATH!s}")
    return DMG_PATH


if __name__ == "__main__":
    build()
