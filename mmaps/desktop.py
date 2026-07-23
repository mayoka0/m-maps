"""Native desktop app entry for M Maps (macOS only).

Architecture (two processes — required so the window is a normal user GUI):

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

The spoof engine, modes, and HTTP API are unchanged — this is only a new
front door. No browser, no terminal.
"""
from __future__ import annotations

import atexit
import argparse
import os
import pwd
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

import warnings

warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL 1.1.1+")

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
WINDOW_TITLE = "M Maps"
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 840

# Parent of the mmaps package. In a source checkout this is the repo root; in
# the portable .app this is Contents/Resources/app. Web/data assets live under
# the package itself (Path(__file__).parent / …), so they always resolve.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _pid_file_path() -> Path:
    """Writable PID path — never inside a read-only .app under /Applications.

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
    if os.geteuid() == 0:
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

    Important: after ``bind((host, 0))`` the kernel assigns a real port — we
    must return ``getsockname()[1]``, never the literal 0. Returning 0 was the
    desktop launch timeout (window URL became http://127.0.0.1:0).
    """
    if preferred and preferred > 0:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((HOST, preferred))
                # Preferred was free; return it (not getsockname needed — we asked for it).
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
# PID file — structural guard against zombie elevated servers
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


def _server_only_match_pattern() -> str:
    """pgrep/pkill pattern unique to elevated M Maps desktop servers."""
    # Match this venv's python running our server-only entry.
    return f"{sys.executable} -m mmaps.desktop --server-only"


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

def run_server_only(port: int) -> int:
    """Blocking server process. Invoked only after admin elevation."""
    if port <= 0:
        print(f"Internal error: invalid server port {port!r}.", file=sys.stderr)
        return 1

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
    python = sys.executable
    cmd = [
        python,
        "-m",
        "mmaps.desktop",
        "--server-only",
        "--port",
        str(port),
    ]
    pythonpath = _pythonpath_for_child()
    start = (
        f"export SUDO_USER={shlex.quote(user)}; "
        f"export USER={shlex.quote(user)}; "
        f"export HOME={shlex.quote(home)}; "
        f"export PYTHONPATH={shlex.quote(pythonpath)}; "
        f"cd {shlex.quote(str(PROJECT_ROOT))} && "
        + " ".join(shlex.quote(part) for part in cmd)
    )
    # Same elevated shell: reap zombies first, then start — one password prompt.
    return _shell_kill_stale_servers() + start


def start_elevated_server(port: int) -> subprocess.Popen:
    """Show the macOS password dialog and start the root server.

    Returns the ``osascript`` Popen. It stays alive until the elevated server
    exits (window close → POST /shutdown → server stops → osascript ends).

    AppleScript's default event timeout is ~2 minutes — far too short for a
    map session — so we wrap the shell in a multi-day timeout.
    """
    shell = _elevated_server_shell(port)
    apple = (
        "with timeout of 604800 seconds\n"  # 7 days
        f"  do shell script {_apple_script_string(shell)} with administrator privileges\n"
        "end timeout"
    )
    try:
        # Don't capture stdout/stderr — keep the child simple. Errors surface
        # in the map UI via /status if the device/tunnel fails after start.
        return subprocess.Popen(["osascript", "-e", apple])
    except FileNotFoundError:
        print("osascript not found — this app only runs on macOS.", file=sys.stderr)
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

def _configure_macos_app_identity() -> None:
    """Make menu bar / Dock show "M Maps" when the host binary is system python3.

    The portable launcher runs Command Line Tools ``python3 -m mmaps.desktop``.
    Without this, Cocoa treats the process as Python.app (mainBundle from the
    interpreter), so the menu bar and sometimes the Dock flip to "Python" after
    the GUI starts — even though Info.plist on the .app is correct.

    Safe no-op outside a .app launch or if PyObjC is unavailable. Never raises.
    """
    if sys.platform != "darwin":
        return
    app_name = os.environ.get("MMAPS_APP_NAME") or WINDOW_TITLE
    try:
        from Foundation import NSProcessInfo

        NSProcessInfo.processInfo().setProcessName_(app_name)
    except Exception:
        pass
    try:
        from Foundation import NSBundle

        bundle = NSBundle.mainBundle()
        info = bundle.infoDictionary() if bundle is not None else None
        if info is not None:
            # mainBundle is usually Python.framework when launched via system python3.
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


def _apply_macos_dock_icon() -> None:
    """Point the Dock at AppIcon.icns inside the .app when we know its path."""
    if sys.platform != "darwin":
        return
    app_bundle = os.environ.get("MMAPS_APP_BUNDLE") or ""
    if not app_bundle:
        return
    icon_path = Path(app_bundle) / "Contents" / "Resources" / "AppIcon.icns"
    if not icon_path.is_file():
        return
    try:
        from AppKit import NSApplication, NSImage

        app = NSApplication.sharedApplication()
        image = NSImage.alloc().initWithContentsOfFile_(str(icon_path))
        if image is not None:
            app.setApplicationIconImage_(image)
    except Exception:
        pass


def _open_window(url: str) -> None:
    import webview

    # Identity must be set before Cocoa finishes configuring the app menu.
    _configure_macos_app_identity()
    _apply_macos_dock_icon()

    webview.create_window(
        WINDOW_TITLE,
        url,
        width=WINDOW_WIDTH,
        height=WINDOW_HEIGHT,
        min_size=(720, 480),
        confirm_close=False,
        text_select=True,
    )
    # Cocoa + WebKit; private_mode=False keeps localStorage (map theme).
    webview.start(gui="cocoa", private_mode=False)


def run_desktop(port: int) -> int:
    """User-facing entry: elevate the server, open the window, shut down cleanly."""
    _ensure_project_on_path()
    # Before admin dialog / webview: claim M Maps identity (not "Python").
    _configure_macos_app_identity()

    # Resolve a real free port up front (never 0). Prefer the requested port
    # when free; otherwise take an OS-assigned ephemeral port.
    # Note: a zombie may still hold DEFAULT_PORT — elevated start kills it first,
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


def main(argv: Optional[List[str]] = None) -> int:
    if sys.platform != "darwin":
        print("M Maps only runs on macOS.", file=sys.stderr)
        return 1

    parser = argparse.ArgumentParser(description="M Maps desktop app")
    parser.add_argument(
        "--server-only",
        action="store_true",
        help=argparse.SUPPRESS,  # elevated child only
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"localhost port for the embedded server (default {DEFAULT_PORT})",
    )
    args = parser.parse_args(argv)

    if args.server_only:
        if os.geteuid() != 0:
            print("Internal error: --server-only must run as root.", file=sys.stderr)
            return 1
        # Elevated server has no GUI — leave process name alone.
        return run_server_only(args.port)

    _configure_macos_app_identity()
    return run_desktop(args.port)


if __name__ == "__main__":
    sys.exit(main())
