"""Map low-level exceptions to short, human UI strings.

Never show bare exception class names like ``DeviceNotFoundError:`` to the user.
"""
from __future__ import annotations


# Exception type names (and substrings of the message) → user-facing text.
_BY_TYPE = {
    "DeviceNotFoundError": (
        "Lost the connection to the iPhone — check the USB cable, unlock the phone, and wait a moment."
    ),
    "NoDeviceConnectedError": (
        "No iPhone on USB — plug it in, unlock it, and keep the cable seated."
    ),
    "NoDeviceError": (
        "No iPhone on USB — plug it in, unlock it, and keep the cable seated."
    ),
    "PasswordRequiredError": (
        "The iPhone is locked or busy — unlock it and keep it awake."
    ),
    "PasscodeRequiredError": (
        "The iPhone needs its passcode — unlock it and try again."
    ),
    "RootRequiredError": (
        "Administrator permission is required to talk to the iPhone."
    ),
    "ConnectionTerminatedError": (
        "Lost the connection to the iPhone — reconnecting if possible."
    ),
    "ChannelClosedError": (
        "Lost the connection to the iPhone — reconnecting if possible."
    ),
    "ConnectionFailedError": (
        "Couldn’t reach the iPhone — check the USB cable."
    ),
    "InvalidConnectionError": (
        "Couldn’t reach the iPhone — check the USB cable."
    ),
}


def humanize_error(error: BaseException) -> str:
    """Return a clear message for banners; never a bare ``ClassName:`` string."""
    name = type(error).__name__
    if name in _BY_TYPE:
        return _BY_TYPE[name]
    detail = str(error).strip()
    if detail:
        # Drop a redundant "ClassName: " prefix if the library put one in.
        prefix = name + ": "
        if detail.startswith(prefix):
            detail = detail[len(prefix):].strip()
        if detail:
            return detail
    if name in ("OSError", "ConnectionError", "TimeoutError"):
        return "Lost the connection to the iPhone — check the USB cable."
    return "Something went wrong talking to the iPhone."


def humanize_error_text(text: str) -> str:
    """Humanize a pre-formatted ``ClassName: message`` string (e.g. from status)."""
    if not text:
        return text
    raw = text.strip()
    # "DeviceNotFoundError:" or "DeviceNotFoundError: something"
    if ":" in raw:
        name, _, rest = raw.partition(":")
        name = name.strip()
        rest = rest.strip()
        if name in _BY_TYPE and (not rest or rest == name):
            return _BY_TYPE[name]
        if name in _BY_TYPE and not rest:
            return _BY_TYPE[name]
        if name.endswith("Error") and not rest:
            return _BY_TYPE.get(name, "Something went wrong talking to the iPhone.")
    if raw.endswith("Error") or raw.endswith("Error:"):
        name = raw.rstrip(":").strip()
        if name in _BY_TYPE:
            return _BY_TYPE[name]
    return raw
