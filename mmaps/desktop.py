"""Native desktop app entry for M Maps (macOS only).

Architecture (two processes - required so the window is a normal user GUI):

1. This process (the double-clicked app) runs as the logged-in user and opens a
   pywebview window pointed at the local map server.
2. The map server (same FastAPI app as ``serve``) must be root to create the
   iOS tunnel / utun. We start it through macOS's native password dialog:

       osascript -e 'do shell script "…" with administrator privileges'

Because that elevated process is not a normal child of the window process, it
can outlive the window if we only `terminate()` osascript. We therefore:

- write a PID file from the elevated server,
- kill any stale PID (and matching processes) inside the *same* elevated shell
  that starts a new server (one password prompt),
- on window close, POST /shutdown then wait; fall back to elevated kill if needed.

The spoof engine, modes, and HTTP API are unchanged - this is only a new
front door. No browser, no terminal.
"""
from __future__ import annotations

import atexit
import argparse
import os
import shlex
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Optional, Tuple

try:
    import pwd
except ImportError:  # Windows has no Unix password database module.
    pwd = None  # type: ignore[assignment]

import warnings

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL 1.1.1+")

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
WINDOW_TITLE = "M Maps"
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 840


def _is_frozen() -> bool:
    """True when running inside a PyInstaller bootloader process."""
    return bool(getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"))


def _detect_app_bundle() -> Optional[Path]:
    """Path to M Maps.app when running from a bundled build."""
    env = os.environ.get("MMAPS_APP_BUNDLE") or ""
    if env:
        p = Path(env)
        if p.is_dir():
            return p
    if _is_frozen():
        # …/M Maps.app/Contents/MacOS/<bootloader>
        exe = Path(sys.executable).resolve()
        # MacOS → Contents → .app
        candidate = exe.parent.parent.parent
        if candidate.suffix == ".app" or candidate.name.endswith(".app"):
            return candidate
    try:
        from Foundation import NSBundle

        bundle = NSBundle.mainBundle()
        if bundle is not None:
            path = Path(str(bundle.bundlePath()))
            if path.suffix == ".app" or "M Maps" in path.name:
                return path
    except Exception:
        pass
    return None


def _bootstrap_bundle_env() -> None:
    """Set MMAPS_APP_BUNDLE / MMAPS_APP_NAME for Dock icon + child elevation."""
    os.environ.setdefault("MMAPS_APP_NAME", WINDOW_TITLE)
    if not os.environ.get("MMAPS_APP_BUNDLE"):
        detected = _detect_app_bundle()
        if detected is not None:
            os.environ["MMAPS_APP_BUNDLE"] = str(detected)


# Parent of the mmaps package.
# - Source checkout: repo root
# - PyInstaller: sys._MEIPASS (extracted/collected tree where mmaps/ lives)
# Web/data assets live under the package itself (Path(__file__).parent / …).
if _is_frozen():
    PROJECT_ROOT = Path(getattr(sys, "_MEIPASS"))
else:
    PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _pid_file_path() -> Path:
    """Writable PID path - never inside a read-only .app under /Applications.

    Elevated server runs as root but HOME/SUDO_USER point at the real user, so
    Application Support stays shared between the window process and the server.
    Line 1 = PID, line 2 = port.
    """
    _user, home = _real_user_and_home()
    base = Path(home) / "Library" / "Application Support" / "M Maps"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        import tempfile

        base = Path(tempfile.gettempdir()) / "M-Maps"
        base.mkdir(parents=True, exist_ok=True)
    return base / "server.pid"


def _real_user_and_home() -> Tuple[str, str]:
    """User/home for the pairing cache when the server runs as root."""
    if pwd is None:
        name = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
        return name, str(Path.home())
    if getattr(os, "geteuid", lambda: -1)() == 0:
        name = os.environ.get("SUDO_USER") or os.environ.get("USER")
        if name and name != "root":
            try:
                info = pwd.getpwnam(name)
                return name, info.pw_dir
            except KeyError:
                pass
    info = pwd.getpwuid(os.getuid())
    return info.pw_name, info.pw_dir


def _apple_script_string(value: str) -> str:
    """Quote a Python string as an AppleScript string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _port_open(host: str, port: int) -> bool:
    if port <= 0:
        return False
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((host, port)) == 0


def _pick_port(preferred: int = DEFAULT_PORT) -> int:
    """Return a free localhost TCP port (always > 0).

    Tries ``preferred`` when it is a positive port and free. Otherwise binds to
    port 0 and returns the OS-assigned ephemeral port.

    Important: after ``bind((host, 0))`` the kernel assigns a real port - we
    must return ``getsockname()[1]``, never the literal 0. Returning 0 was the
    desktop launch timeout (window URL became http://127.0.0.1:0).
    """
    if preferred and preferred > 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((HOST, preferred))
                # Preferred was free; return it (not getsockname needed - we asked for it).
                return preferred
            except OSError:
                pass

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((HOST, 0))
        assigned = int(sock.getsockname()[1])
    if assigned <= 0:
        raise RuntimeError("Could not allocate a free localhost port for the map server.")
    return assigned


def _wait_for_server(host: str, port: int, timeout: float = 180.0) -> bool:
    """Wait until the map server is listening on host:port.

    Password dialog can take a while. Prefers a successful HTTP response, but
    accepts a stable TCP accept after a short grace (lifespan may still be
    connecting the phone while uvicorn is already bound).
    """
    if port <= 0:
        return False
    deadline = time.time() + timeout
    tcp_ok_since: Optional[float] = None
    while time.time() < deadline:
        if _port_open(host, port):
            try:
                urllib.request.urlopen(f"http://{host}:{port}/", timeout=0.5)
                return True
            except urllib.error.HTTPError:
                # Any HTTP status means uvicorn is serving routes.
                return True
            except (urllib.error.URLError, TimeoutError, OSError):
                if tcp_ok_since is None:
                    tcp_ok_since = time.time()
                elif time.time() - tcp_ok_since >= 2.0:
                    return True
        else:
            tcp_ok_since = None
        time.sleep(0.15)
    return False


def _ensure_project_on_path() -> None:
    root = str(PROJECT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    # Keep any launcher-provided vendor path (Resources/vendor in the .app).
    try:
        os.chdir(PROJECT_ROOT)
    except OSError:
        pass


def _pythonpath_for_child() -> str:
    """PYTHONPATH for the elevated server: PROJECT_ROOT + existing vendor path."""
    parts: List[str] = []
    root = str(PROJECT_ROOT)
    existing = os.environ.get("PYTHONPATH") or ""
    for part in existing.split(os.pathsep):
        if part and part not in parts:
            parts.append(part)
    if root not in parts:
        parts.insert(0, root)
    return os.pathsep.join(parts)


# ---------------------------------------------------------------------------
# PID file - structural guard against zombie elevated servers
# ---------------------------------------------------------------------------

def _write_pid_file(port: int) -> None:
    """Record this elevated server's PID + port (called from --server-only)."""
    try:
        path = _pid_file_path()
        path.write_text(f"{os.getpid()}\n{int(port)}\n", encoding="ascii")
    except OSError as e:
        print(f"Warning: could not write PID file ({e}).", file=sys.stderr)


def _read_pid_file() -> Optional[Tuple[int, int]]:
    """Return (pid, port) from the PID file, or None if missing/invalid."""
    try:
        lines = _pid_file_path().read_text(encoding="ascii").strip().splitlines()
    except OSError:
        return None
    if not lines:
        return None
    try:
        pid = int(lines[0].strip())
        port = int(lines[1].strip()) if len(lines) > 1 else 0
    except ValueError:
        return None
    if pid <= 0:
        return None
    return pid, port


def _remove_pid_file_if_ours() -> None:
    """Delete the PID file only if it still points at this process."""
    try:
        data = _read_pid_file()
        if data is None:
            return
        if data[0] == os.getpid():
            _pid_file_path().unlink()
    except OSError:
        pass


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we lack permission to signal it (typical for root).
        return True


# Console helper shipped next to the windowed bootloader (PyInstaller COLLECT).
# Must stay distinct from the GUI binary so elevation is not a second runw/.app.
_SERVER_HELPER_NAME = "mmaps-server"


def _frozen_server_helper() -> Optional[Path]:
    """Path to Contents/MacOS/mmaps-server when running from the .app."""
    if not _is_frozen():
        return None
    helper = Path(sys.executable).resolve().parent / _SERVER_HELPER_NAME
    return helper if helper.is_file() else None


def _server_only_match_pattern() -> str:
    """pgrep/pkill pattern unique to elevated M Maps desktop servers."""
    if _is_frozen():
        helper = _frozen_server_helper()
        if helper is not None:
            return f"{helper} --server-only"
        # Legacy fallback (pre-helper builds): windowed bootloader + flag.
        return f"{sys.executable} --server-only"
    return f"{sys.executable} -m mmaps.desktop --server-only"


def _elevated_server_argv(port: int) -> List[str]:
    """Argv for the root map server (venv module form or frozen helper).

    Frozen builds **must** use the console ``mmaps-server`` helper, not the
    windowed ``M Maps`` bootloader. Re-execing the GUI binary as root created
    a second AppKit-capable process of the same .app and left the window in
    “Application Not Responding”.
    """
    if _is_frozen():
        helper = _frozen_server_helper()
        if helper is None:
            raise RuntimeError(
                "M Maps.app is missing Contents/MacOS/mmaps-server "
                "(headless elevated helper). Rebuild with build_app.py."
            )
        return [str(helper), "--server-only", "--port", str(port)]
    return [
        sys.executable,
        "-m",
        "mmaps.desktop",
        "--server-only",
        "--port",
        str(port),
    ]


def _shell_kill_stale_servers() -> str:
    """Shell snippet (run as root) that kills any previous elevated server."""
    pid_path = shlex.quote(str(_pid_file_path()))
    pattern = shlex.quote(_server_only_match_pattern())
    return (
        f"if [ -f {pid_path} ]; then "
        f"  oldpid=$(sed -n '1p' {pid_path} 2>/dev/null | tr -d '[:space:]'); "
        f"  if [ -n \"$oldpid\" ]; then "
        f"    kill \"$oldpid\" 2>/dev/null || true; "
        f"    sleep 0.4; "
        f"    kill -9 \"$oldpid\" 2>/dev/null || true; "
        f"  fi; "
        f"  rm -f {pid_path}; "
        f"fi; "
        # Sweep any orphaned elevated servers matching this project/venv.
        f"pkill -f {pattern} 2>/dev/null || true; "
        f"sleep 0.25; "
    )


# ---------------------------------------------------------------------------
# Elevated child: the existing FastAPI server (root, no window)
# ---------------------------------------------------------------------------

def _become_headless_server_process() -> None:
    """Keep the elevated helper from acting like a second GUI app instance.

    The console ``mmaps-server`` bootloader already avoids runw; this is a
    belt-and-suspenders policy if anything still touches AppKit.
    """
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyProhibited

        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyProhibited)
    except Exception:
        pass


def run_server_only(port: int) -> int:
    """Blocking headless server. Invoked only after admin elevation."""
    if port <= 0:
        print(f"Internal error: invalid server port {port!r}.", file=sys.stderr)
        return 1

    # Never claim Dock / activation - this process is root + headless only.
    _become_headless_server_process()
    _ensure_project_on_path()

    user, home = _real_user_and_home()
    os.environ.setdefault("SUDO_USER", user)
    os.environ.setdefault("HOME", home)

    import uvicorn

    from mmaps.server import app

    # Desktop mode: allow the user-facing window process to ask us to exit.
    app.state.desktop_mode = True

    config = uvicorn.Config(app, host=HOST, port=port, log_level="warning")
    server = uvicorn.Server(config)
    # Expose for POST /shutdown (see server.py).
    app.state.uvicorn_server = server

    _write_pid_file(port)
    atexit.register(_remove_pid_file_if_ours)

    def _on_signal(signum, frame) -> None:
        # SIGTERM/SIGINT → ask uvicorn to exit; lifespan closes the session.
        server.should_exit = True

    try:
        signal.signal(signal.SIGTERM, _on_signal)
        signal.signal(signal.SIGINT, _on_signal)
    except (ValueError, OSError):
        pass

    try:
        server.run()
    finally:
        _remove_pid_file_if_ours()
    return 0


def _elevated_server_shell(port: int) -> str:
    """Shell command the admin dialog will run (kill stale + start server)."""
    user, home = _real_user_and_home()
    cmd = _elevated_server_argv(port)
    extra_exports = []
    for key in ("MMAPS_APP_BUNDLE", "MMAPS_APP_NAME"):
        val = os.environ.get(key)
        if val:
            extra_exports.append(f"export {key}={shlex.quote(val)}; ")
    # Frozen bootloader does not need PYTHONPATH/PYTHONHOME (everything is
    # inside the .app). Dev/venv still passes PYTHONPATH for vendor + package.
    if not _is_frozen():
        pythonpath = _pythonpath_for_child()
        extra_exports.append(f"export PYTHONPATH={shlex.quote(pythonpath)}; ")
        for key in ("PYTHONHOME",):
            val = os.environ.get(key)
            if val:
                extra_exports.append(f"export {key}={shlex.quote(val)}; ")
        cd_part = f"cd {shlex.quote(str(PROJECT_ROOT))} && "
    else:
        # Stay in a neutral cwd; do not depend on the checkout existing.
        cd_part = "cd /tmp && "
    start = (
        f"export SUDO_USER={shlex.quote(user)}; "
        f"export USER={shlex.quote(user)}; "
        f"export HOME={shlex.quote(home)}; "
        + "".join(extra_exports)
        + cd_part
        + " ".join(shlex.quote(part) for part in cmd)
    )
    # Same elevated shell: reap zombies first, then start - one password prompt.
    return _shell_kill_stale_servers() + start


def start_elevated_server(port: int) -> subprocess.Popen:
    """Show the macOS password dialog and start the root server.

    Returns the ``osascript`` Popen. It stays alive until the elevated server
    exits (window close → POST /shutdown → server stops → osascript ends).

    AppleScript's default event timeout is ~2 minutes - far too short for a
    map session - so we wrap the shell in a multi-day timeout.
    """
    shell = _elevated_server_shell(port)
    apple = (
        "with timeout of 604800 seconds\n"  # 7 days
        f"  do shell script {_apple_script_string(shell)} with administrator privileges\n"
        "end timeout"
    )
    try:
        # Don't capture stdout/stderr - keep the child simple. Errors surface
        # in the map UI via /status if the device/tunnel fails after start.
        return subprocess.Popen(["osascript", "-e", apple])
    except FileNotFoundError:
        print("osascript not found - this app only runs on macOS.", file=sys.stderr)
        sys.exit(1)


def _shutdown_server(port: int) -> None:
    """Ask the elevated server to exit via HTTP (user cannot SIGTERM root)."""
    if port <= 0:
        return
    url = f"http://{HOST}:{port}/shutdown"
    try:
        req = urllib.request.Request(url, method="POST", data=b"")
        urllib.request.urlopen(req, timeout=3)
    except (urllib.error.URLError, TimeoutError, OSError):
        pass


def _wait_until_dead(port: int, pid: Optional[int], timeout: float = 6.0) -> bool:
    """True if the server port is closed and (if known) the PID is gone."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        port_up = _port_open(HOST, port) if port > 0 else False
        pid_up = _pid_is_alive(pid) if pid else False
        if not port_up and not pid_up:
            return True
        time.sleep(0.15)
    return False


def _elevated_force_kill() -> None:
    """Last resort: admin shell that kills by PID file + process pattern.

    May show a password dialog if the auth ticket from launch has expired.
    Prefer HTTP /shutdown; this is only if the server ignored it.
    """
    shell = _shell_kill_stale_servers() + "true"
    apple = f"do shell script {_apple_script_string(shell)} with administrator privileges"
    try:
        subprocess.run(["osascript", "-e", apple], check=False, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        print(f"Warning: could not force-kill stale server ({e}).", file=sys.stderr)


# ---------------------------------------------------------------------------
# User process: native window
# ---------------------------------------------------------------------------

def _app_display_name() -> str:
    return os.environ.get("MMAPS_APP_NAME") or WINDOW_TITLE


def _configure_macos_app_identity() -> None:
    """Claim the process name / bundle strings used by Cocoa for UI chrome.

    The portable launcher runs system ``python3``, so mainBundle is Python's.
    That is necessary but **not sufficient** for the menu-bar app name - see
    ``_patch_pywebview_cocoa_identity`` and ``_force_menu_bar_app_name``.
    Never raises.
    """
    if sys.platform != "darwin":
        return
    app_name = _app_display_name()
    try:
        from Foundation import NSProcessInfo

        NSProcessInfo.processInfo().setProcessName_(app_name)
    except Exception:
        pass
    try:
        from Foundation import NSBundle

        bundle = NSBundle.mainBundle()
        if bundle is None:
            return
        # Patch both dictionaries - cocoa may bind to either at import time.
        for getter in ("infoDictionary", "localizedInfoDictionary"):
            try:
                info = getattr(bundle, getter)()
            except Exception:
                info = None
            if info is None:
                continue
            info["CFBundleName"] = app_name
            info["CFBundleDisplayName"] = app_name
            ident = str(info.get("CFBundleIdentifier") or "")
            if (
                not ident
                or "python" in ident.lower()
                or ident.startswith("org.python")
                or ident.startswith("com.apple.python")
            ):
                info["CFBundleIdentifier"] = "local.mmaps.app"
    except Exception:
        pass


def _patch_pywebview_cocoa_identity() -> None:
    """Fix the *actual* sources of the menu-bar name in pywebview's Cocoa backend.

    Investigation (webview/platforms/cocoa.py):

    1. At **import**, cocoa binds a module-global ``info`` from
       ``mainBundle().localizedInfoDictionary() or infoDictionary()``.
       When the host is system python3, that is Python.app → CFBundleName
       "Python".

    2. ``BrowserView._append_app_name`` builds "Quit …" / "Hide …" / About
       from that **module global**, not a live bundle re-read. Patching only
       ``infoDictionary()`` after import does not help if ``info`` already
       points at a snapshot that still says Python - which is why the prior
       "fix" looked successful in isolation but the menu bar did not.

    3. ``_add_app_menu`` creates the application menu item with **no title**
       and an empty submenu title. AppKit then fills the bold menu-bar label
       from the process/bundle name ("Python").

    We fix all three: rewrite ``cocoa.info``, replace ``_append_app_name`` so
    menu strings always use "M Maps", and wrap ``_add_app_menu`` so the first
    main-menu item is titled "M Maps" as soon as the menu is built.

    Call **immediately after** ``import webview``. Never raises.
    """
    if sys.platform != "darwin":
        return
    app_name = _app_display_name()
    try:
        import webview.platforms.cocoa as cocoa  # type: ignore
    except Exception:
        return

    try:
        info = getattr(cocoa, "info", None)
        if info is not None:
            info["CFBundleName"] = app_name
            info["CFBundleDisplayName"] = app_name
            try:
                info["CFBundleIdentifier"] = "local.mmaps.app"
            except Exception:
                pass
    except Exception:
        pass

    try:
        def _append_app_name(self, val):  # noqa: ANN001
            return f"{val} {app_name}"

        cocoa.BrowserView._append_app_name = _append_app_name  # type: ignore[method-assign]
    except Exception:
        pass

    try:
        if getattr(cocoa.BrowserView, "_mmaps_app_menu_patched", False):
            return
        _orig_add_app_menu = cocoa.BrowserView._add_app_menu

        def _add_app_menu(self, mainMenu, custom_items=None):  # noqa: ANN001
            _orig_add_app_menu(self, mainMenu, custom_items)
            try:
                if mainMenu is None or mainMenu.numberOfItems() < 1:
                    return
                item = mainMenu.itemAtIndex_(0)
                if item is None:
                    return
                item.setTitle_(app_name)
                sub = item.submenu()
                if sub is not None:
                    sub.setTitle_(app_name)
            except Exception:
                pass

        cocoa.BrowserView._add_app_menu = _add_app_menu  # type: ignore[method-assign]
        cocoa.BrowserView._mmaps_app_menu_patched = True  # type: ignore[attr-defined]
    except Exception:
        pass


def _force_menu_bar_app_name() -> bool:
    """Retitle the application menu item if it already exists (belt-and-suspenders).

    Returns True if the first main-menu item title is "M Maps". Never raises.
    """
    if sys.platform != "darwin":
        return False
    app_name = _app_display_name()
    try:
        from AppKit import NSApplication
        from PyObjCTools import AppHelper

        def _apply() -> None:
            app = NSApplication.sharedApplication()
            menu = app.mainMenu()
            if menu is None or menu.numberOfItems() < 1:
                return
            item = menu.itemAtIndex_(0)
            if item is None:
                return
            item.setTitle_(app_name)
            sub = item.submenu()
            if sub is not None:
                sub.setTitle_(app_name)

        try:
            AppHelper.callAfter(_apply)
        except Exception:
            pass
        _apply()
        menu = NSApplication.sharedApplication().mainMenu()
        if menu is not None and menu.numberOfItems() >= 1:
            title = str(menu.itemAtIndex_(0).title() or "")
            return title == app_name
        return False
    except Exception:
        return False


def _app_icon_path() -> Optional[Path]:
    candidates: List[Path] = []
    app_bundle = os.environ.get("MMAPS_APP_BUNDLE") or ""
    if app_bundle:
        candidates.append(Path(app_bundle) / "Contents" / "Resources" / "AppIcon.icns")
    detected = _detect_app_bundle()
    if detected is not None:
        candidates.append(detected / "Contents" / "Resources" / "AppIcon.icns")
    try:
        from Foundation import NSBundle

        bundle = NSBundle.mainBundle()
        if bundle is not None:
            for name in ("AppIcon", "icon-windowed", "icon"):
                p = bundle.pathForResource_ofType_(name, "icns")
                if p:
                    candidates.append(Path(str(p)))
            res = Path(str(bundle.resourcePath() or ""))
            if res.is_dir():
                candidates.extend(res.glob("*.icns"))
    except Exception:
        pass
    for path in candidates:
        if path.is_file():
            return path
    return None


def _apply_macos_dock_icon() -> bool:
    """Optionally set Dock icon - only when the bundle icon is not available.

    Frozen .app builds must **not** call ``setApplicationIconImage_`` with a
    static PNG/ICNS. That replaces the system-managed Dock icon and blocks
    macOS appearance modes (tint / clear / dark). The Dock should use
    ``CFBundleIconFile`` / ``CFBundleIconName`` from Info.plist so the icon
    follows System Settings like every other app.

    Dev (non-frozen) runs have no bundle icon, so we still set it from disk.
    """
    if sys.platform != "darwin":
        return False
    # Packaged app: leave Dock icon to Launch Services + system styling.
    if _is_frozen():
        return True
    icon_path = _app_icon_path()
    if icon_path is None:
        return False
    try:
        from AppKit import NSApplication, NSImage

        app = NSApplication.sharedApplication()
        image = NSImage.alloc().initWithContentsOfFile_(str(icon_path))
        if image is None:
            return False
        try:
            if image.size().width <= 0 or image.size().height <= 0:
                image.setSize_((128.0, 128.0))
        except Exception:
            pass
        app.setApplicationIconImage_(image)
        current = app.applicationIconImage()
        return current is not None
    except Exception:
        return False


def _configure_macos_unified_titlebar(window: object) -> bool:
    """Extend web content beneath a transparent native title bar.

    This removes the otherwise empty row above M Maps without replacing the
    real macOS window chrome. Close, minimize, zoom/full-screen, and normal
    window behavior remain owned by AppKit. Cocoa UI changes are scheduled on
    its main thread because pywebview runs ``func=`` callbacks separately.
    """
    if sys.platform != "darwin":
        return False
    try:
        from AppKit import (
            NSWindowCloseButton,
            NSWindowMiniaturizeButton,
            NSWindowTitleHidden,
            NSWindowZoomButton,
        )
        from PyObjCTools import AppHelper
    except Exception:
        return False

    native = getattr(window, "native", None)
    if native is None:
        return False

    def _apply() -> None:
        try:
            # pywebview's frameless Cocoa mode supplies the full-size/textured
            # window masks that make the title bar genuinely transparent. It
            # hides the standard buttons by default; restore those real AppKit
            # controls instead of drawing replacements in HTML.
            native.setTitlebarAppearsTransparent_(True)
            native.setTitleVisibility_(NSWindowTitleHidden)
            for button_type in (
                NSWindowCloseButton,
                NSWindowMiniaturizeButton,
                NSWindowZoomButton,
            ):
                button = native.standardWindowButton_(button_type)
                if button is not None:
                    button.setHidden_(False)
            # macOS 11+ can otherwise leave a faint separator where the old
            # title bar ended. Zero is NSWindowTitlebarSeparatorStyleNone.
            if hasattr(native, "setTitlebarSeparatorStyle_"):
                native.setTitlebarSeparatorStyle_(0)
        except Exception:
            pass

    try:
        AppHelper.callAfter(_apply)
        return True
    except Exception:
        return False


def _open_window(url: str) -> None:
    # CRITICAL order: claim process/bundle identity BEFORE pywebview imports
    # cocoa (which snapshots CFBundleName and creates NSApplication).
    _configure_macos_app_identity()

    import webview

    # Patch cocoa's module-global info + menu builders (real menu-bar source).
    _patch_pywebview_cocoa_identity()
    _configure_macos_app_identity()
    _apply_macos_dock_icon()

    # The page uses this local-only marker to reserve space for the native
    # traffic lights after the title bar is made transparent. Browser `serve`
    # sessions keep their existing edge-to-edge command bar.
    desktop_url = url + ("&" if "?" in url else "?") + "desktop=1"

    native_window = webview.create_window(
        WINDOW_TITLE,
        desktop_url,
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        min_size=(720, 480),
        confirm_close=False,
        text_select=True,
        # Cocoa's frameless mode keeps the native titled window but makes its
        # content full-size. `_configure_macos_unified_titlebar` restores the
        # traffic lights that pywebview normally hides in this mode.
        frameless=True,
        easy_drag=False,
    )

    def _on_gui_ready() -> None:
        _configure_macos_app_identity()
        _force_menu_bar_app_name()
        _apply_macos_dock_icon()
        _configure_macos_unified_titlebar(native_window)
        try:
            import threading

            def _retry() -> None:
                time.sleep(0.35)
                _force_menu_bar_app_name()
                _apply_macos_dock_icon()
                _configure_macos_unified_titlebar(native_window)
                time.sleep(0.75)
                _force_menu_bar_app_name()
                _apply_macos_dock_icon()
                _configure_macos_unified_titlebar(native_window)

            threading.Thread(target=_retry, daemon=True).start()
        except Exception:
            pass

    # Cocoa + WebKit; private_mode=False keeps localStorage (map theme).
    webview.start(gui="cocoa", private_mode=False, func=_on_gui_ready)


def run_desktop(port: int) -> int:
    """User-facing entry: elevate the server, open the window, shut down cleanly."""
    _ensure_project_on_path()
    # Before admin dialog / webview import: claim M Maps identity (not "Python").
    _configure_macos_app_identity()

    # Resolve a real free port up front (never 0). Prefer the requested port
    # when free; otherwise take an OS-assigned ephemeral port.
    # Note: a zombie may still hold DEFAULT_PORT - elevated start kills it first,
    # so we re-check after launch if needed. Prefer fixed default when free now.
    try:
        port = _pick_port(port if port and port > 0 else DEFAULT_PORT)
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    url = f"http://{HOST}:{port}"
    print(f"Starting M Maps server at {url} …")
    # Elevated shell kills any stale PID/pkill match, then starts this port.
    proc = start_elevated_server(port)

    # Password dialog is modal; wait until the user accepts and the server is
    # actually listening on the same port we will open in the window.
    if not _wait_for_server(HOST, port, timeout=180.0):
        if proc.poll() is not None:
            print(
                "Administrator permission was not granted, or the server failed to start.",
                file=sys.stderr,
            )
            return 1
        print(f"Timed out waiting for the server at {url}", file=sys.stderr)
        proc.terminate()
        return 1

    # Record port for shutdown (PID file is owned by the elevated process).
    server_pid: Optional[int] = None
    data = _read_pid_file()
    if data:
        server_pid, pid_port = data
        if pid_port > 0:
            port = pid_port
            url = f"http://{HOST}:{port}"

    try:
        _open_window(url)
    finally:
        # 1) Polite HTTP shutdown (server is root; this works without kill perms).
        _shutdown_server(port)
        if not _wait_until_dead(port, server_pid, timeout=5.0):
            # 2) Re-read PID (server may have written it) and force-kill elevated.
            data = _read_pid_file()
            if data:
                server_pid = data[0]
            print("Server did not exit cleanly; force-stopping elevated process…", file=sys.stderr)
            _elevated_force_kill()
            _wait_until_dead(port, server_pid, timeout=4.0)

        # 3) Reap the osascript wrapper.
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()

        # Stale PID file if server died hard.
        data = _read_pid_file()
        if data and not _pid_is_alive(data[0]):
            try:
                _pid_file_path().unlink()
            except OSError:
                pass
    return 0


def _run_bundle_smoke() -> int:
    """No-GUI check used by build_app.py: imports + mainBundle identity."""
    import traceback

    _bootstrap_bundle_env()
    lines: List[str] = []
    try:
        import mmaps.desktop  # noqa: F401
        import mmaps.server  # noqa: F401
        import pymobiledevice3  # noqa: F401
        import webview  # noqa: F401

        lines.append("imports_ok")
    except Exception as e:
        print(f"bundle_smoke_fail imports: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1

    # Asset paths used by the map + airport lookup must resolve inside the bundle.
    try:
        from mmaps.server import _WEB_DIR
        from mmaps.airports import _DATA_PATH

        if not (_WEB_DIR / "index.html").is_file():
            print(f"bundle_smoke_fail missing web UI: {_WEB_DIR}", file=sys.stderr)
            return 1
        if not _DATA_PATH.is_file():
            print(f"bundle_smoke_fail missing airports: {_DATA_PATH}", file=sys.stderr)
            return 1
        lines.append("assets_ok")
    except Exception as e:
        print(f"bundle_smoke_fail assets: {e}", file=sys.stderr)
        return 1

    try:
        from Foundation import NSBundle

        bundle = NSBundle.mainBundle()
        bpath = str(bundle.bundlePath()) if bundle else ""
        info = (bundle.infoDictionary() or {}) if bundle else {}
        name = str(info.get("CFBundleName") or "")
        lines.append(f"bundlePath={bpath}")
        lines.append(f"CFBundleName={name}")
        lines.append(f"frozen={_is_frozen()}")
        lines.append(f"executable={sys.executable}")
        if "M Maps.app" not in bpath and not bpath.endswith("M Maps.app"):
            # Also accept when running the bootloader by path before LS full reg.
            if "M Maps" not in bpath and "M Maps" not in sys.executable:
                print(
                    f"bundle_smoke_fail identity: bundlePath={bpath!r}",
                    file=sys.stderr,
                )
                return 1
        if name and name != "M Maps" and "python" in name.lower():
            print(f"bundle_smoke_fail CFBundleName={name!r}", file=sys.stderr)
            return 1
        helper = _frozen_server_helper()
        if _is_frozen():
            if helper is None:
                print(
                    "bundle_smoke_fail missing mmaps-server helper next to bootloader",
                    file=sys.stderr,
                )
                return 1
            lines.append(f"server_helper={helper}")
        lines.append("bundle_ok")
    except Exception as e:
        print(f"bundle_smoke_fail cocoa: {e}", file=sys.stderr)
        return 1

    print("\n".join(lines))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    if sys.platform != "darwin":
        print("M Maps only runs on macOS.", file=sys.stderr)
        return 1

    _bootstrap_bundle_env()

    parser = argparse.ArgumentParser(description="M Maps desktop app")
    parser.add_argument(
        "--server-only",
        action="store_true",
        help=argparse.SUPPRESS,  # elevated child only
    )
    parser.add_argument(
        "--bundle-smoke",
        action="store_true",
        help=argparse.SUPPRESS,  # build-time identity / import check
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"localhost port for the embedded server (default {DEFAULT_PORT})",
    )
    args = parser.parse_args(argv)

    if args.bundle_smoke:
        return _run_bundle_smoke()

    if args.server_only:
        if os.geteuid() != 0:
            print("Internal error: --server-only must run as root.", file=sys.stderr)
            return 1
        # Headless elevated path - no Dock identity patching, no webview.
        return run_server_only(args.port)

    # GUI process only from here.
    if _is_frozen() and _frozen_server_helper() is None:
        print(
            "M Maps.app is incomplete (missing Contents/MacOS/mmaps-server). "
            "Rebuild with: venv/bin/python3 build_app.py",
            file=sys.stderr,
        )
        return 1

    _configure_macos_app_identity()
    return run_desktop(args.port)


if __name__ == "__main__":
    sys.exit(main())
