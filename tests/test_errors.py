"""Offline tests for humanize_error."""
from mmaps.errors import humanize_error, humanize_error_text


class DeviceNotFoundError(Exception):
    pass


def test_known_type():
    msg = humanize_error(DeviceNotFoundError("whatever"))
    assert "USB" in msg or "iPhone" in msg
    assert "DeviceNotFoundError" not in msg


def test_string_form():
    out = humanize_error_text("DeviceNotFoundError:")
    assert "DeviceNotFoundError" not in out or "USB" in out


def test_empty_message_fallback():
    class WeirdError(Exception):
        def __str__(self):
            return ""

    msg = humanize_error(WeirdError())
    assert "iPhone" in msg
