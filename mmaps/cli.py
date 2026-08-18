"""Command-line entry point for M Maps.

Usage:
    python3 m_maps.py detect                  # no root needed
    sudo venv/bin/python3 m_maps.py set [LAT LON]
    sudo venv/bin/python3 m_maps.py clear

`set` and `clear` need root because starting the iOS 17+ tunnel creates a
virtual network interface (utun) on the Mac, which macOS restricts to root.
`detect` only talks to usbmuxd/lockdownd and needs no special privileges.
"""
import argparse
import asyncio
import math
import os
import sys
import threading
import warnings

# Harmless on macOS's system Python: LibreSSL vs. OpenSSL version notice from
# urllib3, unrelated to anything this tool does.
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL 1.1.1+")

from pymobiledevice3.exceptions import PasscodeRequiredError, PasswordRequiredError

from mmaps import device, location
from mmaps.errors import humanize_error


def read_coordinates(args) -> tuple:
    if args.latitude is not None and args.longitude is not None:
        lat, lon = args.latitude, args.longitude
    else:
        print("Enter the coordinates to spoof.")
        lat = _read_float("Latitude (-90 to 90): ")
        lon = _read_float("Longitude (-180 to 180): ")

    if not (math.isfinite(lat) and -90.0 <= lat <= 90.0):
        raise SystemExit(f"Latitude {lat} is out of range (-90 to 90).")
    if not (math.isfinite(lon) and -180.0 <= lon <= 180.0):
        raise SystemExit(f"Longitude {lon} is out of range (-180 to 180).")
    return lat, lon


def _read_float(prompt: str) -> float:
    while True:
        raw = input(prompt).strip()
        try:
            return float(raw)
        except ValueError:
            print("That's not a number, try again.")


def require_root(command: str) -> None:
    if os.geteuid() != 0:
        raise SystemExit(
            f"'{command}' needs root to start the location tunnel. Run it as:\n"
            f"  sudo {sys.executable} {os.path.abspath(sys.argv[0])} {command}"
        )


async def cmd_detect(args) -> int:
    client = await device.connect(autopair=False)
    try:
        status = await device.get_status(client)
        device.print_status(status)
    finally:
        await client.close()
    return 0


def _clear_command_hint() -> str:
    return f"sudo {sys.executable} {os.path.abspath(sys.argv[0])} clear"


async def cmd_set(args) -> int:
    lat, lon = read_coordinates(args)

    client = await device.connect(autopair=False)
    try:
        client = await device.ensure_trusted(client)
        if not await device.ensure_developer_mode(client):
            print("Developer Mode is required to set a location. Stopping here.")
            return 1

        print()
        device.print_status(await device.get_status(client))

        print()
        print(f"Starting tunnel and holding location at {lat}, {lon}...")

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        # Read Enter on a daemon thread: while the tunnel is dropping and
        # reconnecting, the main coroutine is busy, so we can't block it on
        # input(). A daemon thread never holds up process exit, and setting the
        # asyncio.Event has to be marshalled back onto the loop thread-safely.
        prompt_shown = False

        def wait_for_enter():
            try:
                input("")
            except (EOFError, KeyboardInterrupt):
                pass
            loop.call_soon_threadsafe(stop_event.set)

        def on_event(kind, info):
            nonlocal prompt_shown
            if kind == "connected":
                print(f"Location set to {lat}, {lon}. Check Maps or Find My on the phone.")
                if not prompt_shown:
                    print("   Holding it active - press Enter to clear and restore the real GPS.")
                    prompt_shown = True
            elif kind == "connection_lost":
                print("Lost the connection - reconnecting...")
            elif kind == "reconnected":
                print("Reconnected - location is active again.")
            elif kind == "cleared":
                print("Cleared. The phone should report its real location again.")
            elif kind == "clear_failed":
                print(f"Warning: couldn't clear cleanly ({info['error']}).")
                print(f"   To force a reset, run: {_clear_command_hint()}")

        threading.Thread(target=wait_for_enter, daemon=True).start()

        # Static teleport: always assert the same point. Route/drive/fly
        # playback will later pass a source that advances over time instead.
        target = (lat, lon)
        await location.hold_location(client, lambda: target, stop_event, on_event=on_event)
        return 0
    finally:
        await client.close()


async def cmd_clear(args) -> int:
    client = await device.connect(autopair=False)
    try:
        client = await device.ensure_trusted(client)
        print("Clearing any simulated location...")
        try:
            await location.clear_via_new_session(client)
            print("Cleared. The phone should report its real location again.")
        except Exception as e:
            print(f"Failed to clear cleanly: {humanize_error(e)}")
            print("Try reconnecting the USB cable and running this again.")
            return 1
        return 0
    finally:
        await client.close()


async def _serve_preflight() -> bool:
    """Verify the phone is ready before starting the server, guiding in-terminal.

    Runs on a throwaway loop and closes its connection; the server reconnects on
    uvicorn's own loop (device sockets must live on the loop that uses them).
    Handling trust / Developer Mode here - including the reboot Developer Mode
    needs - keeps those interactive prompts in the terminal, not the browser.
    """
    # Unified mode is allowed to start with Android only. If an iPhone is not
    # present, defer all iPhone-specific trust checks until it is selected.
    from pymobiledevice3 import usbmux
    if not any(item.is_usb for item in await usbmux.list_devices()):
        return True

    client = await device.connect(autopair=False)
    try:
        client = await device.ensure_trusted(client)
        if not await device.ensure_developer_mode(client):
            print("Developer Mode is required to run the map server. Stopping here.")
            return False
        print()
        device.print_status(await device.get_status(client))
        return True
    finally:
        await client.close()


def _open_browser_when_ready(url: str, host: str, port: int) -> None:
    """Wait for the server to accept connections, then open the browser once.

    Connects to the same host we bind to (localhost or the LAN IP) - binding
    to a LAN address does not also listen on 127.0.0.1.
    """
    import socket
    import time
    import webbrowser

    deadline = time.time() + 15
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((host, port)) == 0:
                webbrowser.open(url)
                return
        time.sleep(0.3)


def cmd_serve(args) -> int:
    platform = "android" if getattr(args, "android", False) else "auto"
    if platform in {"ios", "auto"} and not asyncio.run(_serve_preflight()):
        return 1

    from mmaps import server

    lan = bool(getattr(args, "lan", False))
    if lan:
        try:
            host = server.detect_lan_ip()
        except RuntimeError as e:
            print(f"Error: {e}")
            return 1
    else:
        host = server.HOST

    url = f"http://{host}:{args.port}"
    print()
    if lan:
        print(f"LAN mode: open {url} on any device on this Wi-Fi")
        print()
        print("WARNING: Anyone on this Wi-Fi network can open and control this page.")
        print("         No password - only your home network keeps it private.")
        print("         Stop the server (Ctrl+C) when you are done.")
    else:
        print(f"Starting the M Maps server at {url}")
        print("(localhost only - pass --lan to open it from other devices on your Wi-Fi)")
    print()
    print("Leave this terminal running. Press Ctrl+C to stop and restore your real GPS.")
    if not args.no_browser:
        threading.Thread(
            target=_open_browser_when_ready, args=(url, host, args.port), daemon=True
        ).start()

    try:
        server.run(host=host, port=args.port, lan=lan, platform=platform)
    except KeyboardInterrupt:
        pass
    print()
    print("Server stopped. Reconnect a disconnected Android phone before using Restore GPS if it was still holding a mock location.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="m-maps", description="Spoof the location an iPhone reports over USB.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("detect", help="detect the connected iPhone and report its status (no root needed)")

    set_parser = sub.add_parser("set", help="set the iPhone's simulated location (needs sudo)")
    set_parser.add_argument("latitude", type=float, nargs="?", default=None)
    set_parser.add_argument("longitude", type=float, nargs="?", default=None)

    sub.add_parser("clear", help="clear any simulated location, restoring real GPS (needs sudo)")

    serve_parser = sub.add_parser(
        "serve",
        help="open the unified map GUI for iPhone or Android (iPhone support needs sudo)",
    )
    serve_parser.add_argument("--port", type=int, default=8765, help="local port to serve on (default: 8765)")
    serve_parser.add_argument("--no-browser", action="store_true", help="don't auto-open the browser")
    serve_parser.add_argument(
        "--android",
        action="store_true",
        help="legacy Android-only mode; unified auto-detection is now the default",
    )
    serve_parser.add_argument(
        "--lan",
        action="store_true",
        help="bind to this Mac's Wi-Fi/LAN IP so phones on the same network can open the map "
             "(never the default; anyone on the Wi-Fi can control the page)",
    )

    app_parser = sub.add_parser(
        "app",
        help="open the native desktop app window (macOS; asks for admin via the system password dialog)",
    )
    app_parser.add_argument("--port", type=int, default=8765, help="localhost port for the embedded server (default: 8765)")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command in ("set", "clear") or (
        args.command == "serve" and not getattr(args, "android", False)
    ):
        require_root(args.command)

    try:
        if args.command == "serve":
            return cmd_serve(args)
        if args.command == "app":
            # Desktop entry handles its own elevation (osascript); do not require_root here.
            from mmaps.desktop import main as desktop_main
            return desktop_main(["--port", str(args.port)])
        handlers = {"detect": cmd_detect, "set": cmd_set, "clear": cmd_clear}
        return asyncio.run(handlers[args.command](args))
    except (PasswordRequiredError, PasscodeRequiredError):
        # The phone is locked, so lockdownd won't talk to us.
        print("Unlock your iPhone (and keep it unlocked), then try again.")
        return 1
    except device.NoDeviceError:
        print("No iPhone detected over USB. Plug it in, unlock it, and try again.")
        return 1
    except location.RootRequiredError as e:
        print(f"Error: {e}")
        return 1
    except RuntimeError as e:
        print(f"Error: {e}")
        return 1
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    sys.exit(main())
