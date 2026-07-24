"""PyInstaller runtime hook: stub IPython before pymobiledevice3 imports.

pymobiledevice3.utils does ``import IPython`` at module import time (for an
optional interactive shell we never use). Pulling real IPython + prompt_toolkit
into a windowed .app breaks on import (``TypeError: expected string or
bytes-like object``). We only need the lockdown/tunnel stack, so provide a
minimal stub and exclude the real packages from the build.
"""
from __future__ import annotations

import sys
import types


def _install_stub() -> None:
    if "IPython" in sys.modules:
        return

    ipython = types.ModuleType("IPython")

    def embed(*_args, **_kwargs):  # noqa: ANN001
        raise RuntimeError(
            "Interactive IPython is not available inside M Maps.app "
            "(not needed for spoofing)."
        )

    ipython.embed = embed  # type: ignore[attr-defined]
    sys.modules["IPython"] = ipython

    # Some call paths use IPython.terminal.embed.InteractiveShellEmbed
    terminal = types.ModuleType("IPython.terminal")
    embed_mod = types.ModuleType("IPython.terminal.embed")
    embed_mod.InteractiveShellEmbed = embed  # type: ignore[attr-defined]
    embed_mod.embed = embed  # type: ignore[attr-defined]
    sys.modules["IPython.terminal"] = terminal
    sys.modules["IPython.terminal.embed"] = embed_mod


_install_stub()
