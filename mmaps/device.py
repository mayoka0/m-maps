"""Device detection, trust (pairing), and Developer Mode handling.

Everything here talks to the phone over USB through usbmuxd/lockdownd — no
tunnel, no root required. The iOS 17+ tunnel (needed only to *move* the
location) lives in location.py.
"""
import os
import pwd
from pathlib import Path
from typing import Optional

from pymobiledevice3 import usbmux
from pymobiledevice3.exceptions import (
    AmfiError,
    DeviceHasPasscodeSetError,
    DeveloperModeError,
    NoDeviceConnectedError,
    PairingDialogResponsePendingError,
    UserDeniedPairingError,
)
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.amfi import AmfiService

# How long to wait for the user to tap "Trust" on the phone before giving up.
TRUST_DIALOG_TIMEOUT = 60


def pairing_cache_dir() -> Path:
    """Where pymobiledevice3 caches pair records.

    When this process is run under `sudo`, `Path.home()` resolves to root's
    home directory instead of the invoking user's — which would make every
    sudo invocation look like a fresh, untrusted device even after the user
    already tapped "Trust". We instead resolve the real invoking user's home
    (via $SUDO_USER) so the same cache is used whether or not we're root.
    """
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and os.geteuid() == 0:
        try:
            return Path(pwd.getpwnam(sudo_user).pw_dir) / ".pymobiledevice3"
        except KeyError:
            pass
    return Path.home() / ".pymobiledevice3"


class NoDeviceError(RuntimeError):
    """Raised when no iPhone is visible over USB."""


async def connect(
    autopair: bool = False,
    pair_timeout: Optional[float] = None,
    serial: Optional[str] = None,
):
    """Connect to a USB-attached device over lockdownd.

    :param autopair: if True and the device isn't paired yet, actively request
        pairing — this is what makes the "Trust This Computer?" dialog appear.
    :param serial: usbmux serial (UDID) of the target phone, or ``None`` for
        the first USB device. Prefer an explicit serial when the user confirmed
        a specific phone (two devices on one Mac).
    :raises NoDeviceError: nothing is plugged in / visible over USB.
    """
    devices = await usbmux.list_devices()
    usb = [d for d in devices if getattr(d, "is_usb", False)]
    if not usb:
        raise NoDeviceError("No iPhone detected over USB. Plug it in, unlock it, and try again.")

    target_serial: Optional[str] = None
    if serial:
        want = serial.replace("-", "").strip().lower()
        for d in usb:
            have = (getattr(d, "serial", None) or "").replace("-", "").strip().lower()
            if have and have == want:
                target_serial = d.serial
                break
        if target_serial is None:
            raise NoDeviceError(
                "The confirmed iPhone is not visible over USB. "
                "Plug it in, unlock it, and try again."
            )
    else:
        target_serial = usb[0].serial

    try:
        return await create_using_usbmux(
            serial=target_serial,
            autopair=autopair,
            pair_timeout=pair_timeout,
            pairing_records_cache_folder=pairing_cache_dir(),
        )
    except NoDeviceConnectedError as e:
        raise NoDeviceError("No iPhone detected over USB. Plug it in, unlock it, and try again.") from e


async def get_status(client) -> dict:
    """Collect the human-facing facts about the connected device.

    Developer Mode status requires a trusted/paired connection to read — if
    the device isn't trusted yet, we report it as unknown rather than guess.
    """
    status = {
        "name": client.all_values.get("DeviceName") or "(unknown)",
        "ios_version": client.product_version,
        "product_type": client.all_values.get("ProductType") or "(unknown)",
        "trusted": client.paired,
        "developer_mode": None,
    }
    if client.paired:
        try:
            status["developer_mode"] = await client.get_developer_mode_status()
        except Exception:
            status["developer_mode"] = None
    return status


def print_status(status: dict) -> None:
    print(f"Device:          {status['name']} ({status['product_type']})")
    print(f"iOS version:     {status['ios_version']}")
    print(f"Trusted:         {'yes' if status['trusted'] else 'no'}")
    if status["developer_mode"] is None:
        dev_mode_text = "unknown (can't be read until the device is trusted)"
    else:
        dev_mode_text = "on" if status["developer_mode"] else "off"
    print(f"Developer Mode:  {dev_mode_text}")


async def ensure_trusted(client, *, serial: Optional[str] = None, interactive: bool = True):
    """Make sure the Mac is trusted by the phone.

    When not already paired, reconnects with ``autopair=True`` so iOS shows
    "Trust This Computer?". Works headless (desktop app / elevated server):
    no stdin required — just waits for the phone dialog.

    :param interactive: if True, print CLI guidance (``serve`` / terminal).
    :returns: a (possibly reconnected) paired client.
    """
    if client.paired:
        return client

    serial = serial or getattr(client, "udid", None)

    if interactive:
        print()
        print("This Mac isn't trusted by the phone yet.")
        print(
            'Look at the iPhone screen now: tap "Trust" on the '
            '"Trust This Computer?" prompt and enter your passcode if asked.'
        )
        print(f"Waiting up to {TRUST_DIALOG_TIMEOUT}s for a response...")

    await client.close()
    try:
        client = await connect(
            autopair=True,
            pair_timeout=TRUST_DIALOG_TIMEOUT,
            serial=serial,
        )
    except PairingDialogResponsePendingError as e:
        raise RuntimeError(
            "Timed out waiting for you to tap Trust on the phone. "
            "Unlock the iPhone, keep it plugged in, and try again."
        ) from e
    except UserDeniedPairingError as e:
        raise RuntimeError(
            'Pairing was declined on the phone ("Don\'t Trust"). '
            "Confirm again and tap Trust to proceed."
        ) from e

    if not client.paired:
        raise RuntimeError(
            "Still not trusted after pairing attempt. Unlock the phone and try again."
        )

    if interactive:
        print("Trusted.")
    return client


DEVELOPER_MODE_OFF_MESSAGE = (
    "Developer Mode is OFF on this iPhone (required to spoof location). "
    "On the phone: Settings → Privacy & Security → Developer Mode → turn it on, "
    "allow the restart, then tap Turn On and enter your passcode. "
    "Keep USB plugged in and confirm the device again."
)


async def ensure_developer_mode(
    client,
    auto_enable: bool = True,
    *,
    interactive: bool = True,
) -> bool:
    """Make sure Developer Mode is on.

    Returns True if Developer Mode ends up enabled, False otherwise.
    Headless (desktop) path never uses ``input()`` — it only reports status
    (and may still auto-enable when ``auto_enable=True`` without a prompt).
    """
    try:
        enabled = await client.get_developer_mode_status()
    except Exception as e:
        raise RuntimeError(f"Couldn't read Developer Mode status: {e}") from e

    if enabled:
        return True

    if interactive:
        print()
        print("Developer Mode is OFF on this iPhone. Setting a location requires it.")

    if not auto_enable:
        if interactive:
            _print_manual_developer_mode_steps()
        return False

    if interactive:
        answer = input(
            "Enable Developer Mode now? This will reboot the iPhone. [Y/n] "
        ).strip().lower()
        if answer not in ("", "y", "yes"):
            _print_manual_developer_mode_steps()
            return False
        print("Requesting Developer Mode... the iPhone will reboot. Keep it plugged in.")
    # Desktop / elevated: no stdin — request enable when auto_enable is True.
    # AmfiService handles reboot + reconnect when enable_post_restart=True.

    try:
        await AmfiService(client).enable_developer_mode(enable_post_restart=True)
    except DeviceHasPasscodeSetError as e:
        raise RuntimeError(
            "The device rejected the request because of its passcode state. "
            "Unlock the iPhone and make sure it isn't showing a passcode prompt, then try again."
        ) from e
    except (AmfiError, DeveloperModeError) as e:
        raise RuntimeError(f"Failed to enable Developer Mode: {e}") from e

    if interactive:
        print("Developer Mode enabled and confirmed after restart.")
    return True


def _print_manual_developer_mode_steps() -> None:
    print("To enable it yourself:")
    print("  Settings -> Privacy & Security -> Developer Mode -> turn it on")
    print(
        "  -> the phone will ask to restart -> after restart, tap Turn On, "
        "then enter your passcode."
    )
    print("Then run this tool again.")
