#!/usr/bin/env python3
"""Build a polished macOS .dmg installer for M Maps.

Layout (standard drag-to-install):
  - ``M Maps.app`` on the left
  - shortcut to ``/Applications`` on the right
  - volume icon set to the app icon

Requires a prior ``build_app.py`` run (``dist/M Maps.app``). Output:
``dist/M Maps.dmg``. Uses only macOS built-ins (hdiutil + AppleScript) —
no Homebrew / create-dmg dependency.
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

# Finder window geometry (points) for a clean two-icon layout.
WINDOW_WIDTH = 640
WINDOW_HEIGHT = 400
ICON_SIZE = 128
APP_XY = (160, 180)
APPS_XY = (480, 180)


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
    # e.g. /dev/disk4s1  Apple_HFS  /Volumes/M Maps
    for line in out.splitlines():
        if "/Volumes/" in line:
            vol = line.split("\t")[-1].strip() or line[line.index("/Volumes/") :].strip()
            # last token is often the path
            m = re.search(r"(/Volumes/.+)$", line)
            if m:
                return Path(m.group(1).strip())
            return Path(vol)
    raise RuntimeError(f"Could not find mount point in hdiutil output:\n{out}")


def _set_finder_layout(volume: Path) -> None:
    """Position app + Applications alias and set icon size / window bounds."""
    # AppleScript paths: POSIX for shell, HFS-ish for Finder.
    script = f'''
tell application "Finder"
    tell disk "{VOLUME_NAME}"
        open
        set current view of container window to icon view
        set toolbar visible of container window to false
        set statusbar visible of container window to false
        set the bounds of container window to {{100, 100, {100 + WINDOW_WIDTH}, {100 + WINDOW_HEIGHT}}}
        set viewOptions to the icon view options of container window
        set arrangement of viewOptions to not arranged
        set icon size of viewOptions to {ICON_SIZE}
        set position of item "{APP_NAME}.app" of container window to {{{APP_XY[0]}, {APP_XY[1]}}}
        set position of item "Applications" of container window to {{{APPS_XY[0]}, {APPS_XY[1]}}}
        update without registering applications
        delay 1
        close
        open
        delay 1
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
        # Copy app into staging (preserve bundle).
        shutil.copytree(APP_BUNDLE, stage / f"{APP_NAME}.app", symlinks=True)
        # Drag-to-install target.
        os.symlink("/Applications", stage / "Applications")
        # Volume icon (shown in Finder sidebar / desktop when the DMG is open).
        shutil.copy2(ICON_ICNS, stage / ".VolumeIcon.icns")

        rw_dmg = Path(tmp) / "rw.dmg"
        if DMG_PATH.exists():
            DMG_PATH.unlink()

        # Size the RW image with headroom for Finder metadata.
        # -srcfolder often skips dotfiles; we re-copy the volume icon after attach.
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
            # Layout first (Finder open/close), then volume icon so nothing
            # from the AppleScript pass can race the custom-icon write.
            _set_finder_layout(volume)
            vol_icon = volume / ".VolumeIcon.icns"
            shutil.copy2(ICON_ICNS, vol_icon)
            # Mark the volume as having a custom icon (Xcode CLT SetFile).
            try:
                subprocess.run(
                    ["SetFile", "-a", "C", str(volume)],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except (FileNotFoundError, subprocess.CalledProcessError):
                # Without SetFile the .icns still ships; Finder may show it after remount.
                pass
            if not vol_icon.is_file() or vol_icon.stat().st_size < 1000:
                raise RuntimeError(
                    f"Failed to write .VolumeIcon.icns on {volume} "
                    f"(exists={vol_icon.is_file()})"
                )
            # Flush Finder + filesystem before detach so .DS_Store / icon stick.
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
    print(f"Open:  open {DMG_PATH!s}")
    return DMG_PATH


if __name__ == "__main__":
    build()
