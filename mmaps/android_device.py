"""Host-side controller for the M Maps Android companion.

ADB carries commands over USB to a localhost-only socket inside the companion
app. The phone itself owns the mock-location keepalive, so a held location can
continue after the cable is disconnected.
"""
from __future__ import annotations

import json
import os
import pwd
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence


COMPANION_PACKAGE = "org.mmaps.companion"
COMPANION_DEVICE_PORT = 8765
COMPANION_HOST_PORT = 8766


class AndroidDeviceError(RuntimeError):
    """Base error for friendly Android device failures."""


class AdbNotFoundError(AndroidDeviceError):
    pass


class AndroidDeviceNotFoundError(AndroidDeviceError):
    pass


class AndroidDeviceUnauthorizedError(AndroidDeviceError):
    pass


class CompanionUnavailableError(AndroidDeviceError):
    pass


@dataclass(frozen=True)
class AndroidDevice:
    serial: str
    model: str
    android_version: str
    status: str = "device"


def find_adb() -> str:
    """Find adb without requiring Android Studio to modify the user's PATH."""
    configured = os.environ.get("ANDROID_ADB")
    candidates = [configured, shutil.which("adb")]
    sdk_root = os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME")
    if sdk_root:
        candidates.append(str(Path(sdk_root) / "platform-tools" / "adb"))
    candidates.extend(
        [
            str(Path.home() / "Library" / "Android" / "sdk" / "platform-tools" / "adb"),
            str(Path.home() / "Android" / "Sdk" / "platform-tools" / "adb"),
        ]
    )
    # ``serve`` may be launched with sudo for iPhone tunnel access. Reuse the
    # logged-in user's Android SDK instead of looking only under /var/root.
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        try:
            user_home = Path(pwd.getpwnam(sudo_user).pw_dir)
            candidates.extend([
                str(user_home / "Library" / "Android" / "sdk" / "platform-tools" / "adb"),
                str(user_home / "Android" / "Sdk" / "platform-tools" / "adb"),
            ])
        except KeyError:
            pass
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise AdbNotFoundError("Android platform-tools (adb) are not installed or could not be found.")


class AndroidController:
    """Detect one USB-debugging Android device and send companion commands."""

    def __init__(
        self,
        adb_path: Optional[str] = None,
        host_port: int = COMPANION_HOST_PORT,
        device_port: int = COMPANION_DEVICE_PORT,
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ) -> None:
        self.adb_path = adb_path or find_adb()
        self.host_port = host_port
        self.device_port = device_port
        self._runner = runner
        self._lock = threading.RLock()
        self.device: Optional[AndroidDevice] = None

    def _adb(self, args: Sequence[str], *, serial: Optional[str] = None) -> str:
        command: List[str] = [self.adb_path]
        selected = serial or (self.device.serial if self.device else None)
        if selected:
            command.extend(["-s", selected])
        command.extend(args)
        result = self._runner(command, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise AndroidDeviceError(detail or "ADB command failed.")
        return result.stdout.strip()

    def list_devices(self) -> List[AndroidDevice]:
        """List Android USB states without requiring the companion app.

        Discovery deliberately keeps unauthorized/offline devices in the list
        so the UI can explain what the user needs to fix. Only ready devices
        receive the small model/version property queries.
        """
        output = self._adb(["devices"])
        rows = [line.split() for line in output.splitlines()[1:] if line.strip()]
        devices: List[AndroidDevice] = []
        for row in rows:
            if not row:
                continue
            serial = row[0]
            status = row[1] if len(row) > 1 else "unknown"
            model = "Android"
            version = ""
            if status == "device":
                # One stale or waking device must not hide every other phone
                # from discovery. Keep a usable candidate and retry its
                # properties on the next poll.
                try:
                    model = self._adb(
                        ["shell", "getprop", "ro.product.model"], serial=serial
                    )
                    version = self._adb(
                        ["shell", "getprop", "ro.build.version.release"], serial=serial
                    )
                except (AndroidDeviceError, OSError, subprocess.SubprocessError):
                    model = "Android"
                    version = ""
            devices.append(AndroidDevice(
                serial=serial,
                model=model or "Android",
                android_version=version,
                status=status,
            ))
        return devices

    def detect(self, serial: Optional[str] = None) -> AndroidDevice:
        """Return one ready device, optionally selecting its stable serial."""
        devices = self.list_devices()
        unauthorized = [d.serial for d in devices if d.status == "unauthorized"]
        ready = [d for d in devices if d.status == "device"]
        if not ready and unauthorized:
            raise AndroidDeviceUnauthorizedError(
                "Unlock the Android phone and approve the USB debugging prompt."
            )
        if not ready:
            raise AndroidDeviceNotFoundError(
                "No Android phone is ready over USB. Connect it and enable USB debugging."
            )
        if serial:
            selected = next((d for d in ready if d.serial == serial), None)
            if selected is None:
                raise AndroidDeviceNotFoundError(
                    "The selected Android phone is no longer connected."
                )
            return selected
        if len(ready) > 1:
            raise AndroidDeviceError("More than one Android device is connected; disconnect the extras.")

        self.device = ready[0]
        return self.device

    def connect(self, serial: Optional[str] = None) -> AndroidDevice:
        """Detect the phone, verify the companion, and create the USB port forward."""
        with self._lock:
            device = self.detect(serial=serial or (self.device.serial if self.device else None))
            installed = self._adb(["shell", "pm", "path", COMPANION_PACKAGE])
            if not installed.startswith("package:"):
                raise CompanionUnavailableError("M Maps Companion is not installed on the Android phone.")
            self._adb(["forward", f"tcp:{self.host_port}", f"tcp:{self.device_port}"])
            if not self.companion_reachable():
                # Starting the exported setup activity is allowed over ADB and it
                # starts the non-exported foreground service after permission checks.
                self._adb([
                    "shell", "am", "start", "-n",
                    f"{COMPANION_PACKAGE}/.MainActivity",
                ])
                deadline = time.monotonic() + 4.0
                while time.monotonic() < deadline:
                    if self.companion_reachable():
                        break
                    time.sleep(0.15)
                else:
                    raise CompanionUnavailableError(
                        "M Maps Companion did not start. Open it on the phone and try again."
                    )
            return device

    def companion_reachable(self) -> bool:
        """Whether the phone-side localhost service accepts commands."""
        try:
            self._exchange({"action": "PING"}, timeout=0.7)
            return True
        except CompanionUnavailableError:
            return False

    def _exchange(self, payload: Dict[str, object], *, timeout: float = 3.0) -> None:
        """Send one command and require an acknowledgment from the phone app."""
        with self._lock:
            data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
            try:
                with socket.create_connection(
                    ("127.0.0.1", self.host_port), timeout=timeout
                ) as connection:
                    connection.settimeout(timeout)
                    connection.sendall(data)
                    response = connection.makefile("rb").readline()
                if not response:
                    raise CompanionUnavailableError("M Maps Companion did not acknowledge the command.")
                decoded = json.loads(response.decode("utf-8"))
                if not isinstance(decoded, dict):
                    raise CompanionUnavailableError("M Maps Companion returned an invalid acknowledgment.")
                if decoded.get("ok") is not True:
                    raise CompanionUnavailableError("M Maps Companion rejected the command.")
            except CompanionUnavailableError:
                raise
            except (OSError, ValueError, json.JSONDecodeError) as error:
                raise CompanionUnavailableError(
                    "Could not reach M Maps Companion. Open it on the phone and try again."
                ) from error

    def _send(self, payload: Dict[str, object]) -> None:
        self._exchange(payload)

    def set_location(self, latitude: float, longitude: float, **motion: float) -> None:
        payload: Dict[str, object] = {
            "action": "SET_LOCATION",
            "latitude": float(latitude),
            "longitude": float(longitude),
        }
        for key in ("altitude", "speed", "bearing", "accuracy"):
            if key in motion:
                payload[key] = float(motion[key])
        self._send(payload)

    def clear_location(self) -> None:
        self._send({"action": "CLEAR_LOCATION"})

    def close(self) -> None:
        """Remove only this controller's forwarding rule; keep phone state unchanged."""
        try:
            self._adb(["forward", "--remove", f"tcp:{self.host_port}"])
        except AndroidDeviceError:
            pass
