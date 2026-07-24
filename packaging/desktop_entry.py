#!/usr/bin/env python3
"""PyInstaller entry point for the M Maps desktop app.

Kept separate from mmaps.desktop so Analysis has a stable script path and
the frozen bootloader can re-exec itself with --server-only for elevation.
"""
from __future__ import annotations

from mmaps.desktop import main

if __name__ == "__main__":
    raise SystemExit(main())
