"""Offline tests for Android ADB detection and acknowledged commands."""
import json
import subprocess

import pytest

from mmaps.android_device import (
    AndroidController,
    AndroidDeviceNotFoundError,
    AndroidDeviceUnauthorizedError,
    CompanionUnavailableError,
)


class FakeRunner:
    def __init__(self, devices="SERIAL\tdevice"):
        self.devices = devices
        self.commands = []

    def __call__(self, command, **kwargs):
        self.commands.append(command)
        tail = command[-1]
        if command[-1:] == ["devices"]:
            output = "List of devices attached\n" + self.devices + "\n"
        elif tail == "ro.product.model":
            output = "SM-A156U\n"
        elif tail == "ro.build.version.release":
            output = "16\n"
        elif "path" in command and "org.mmaps.companion" in command:
            output = "package:/data/app/org.mmaps.companion/base.apk\n"
        else:
            output = ""
        return subprocess.CompletedProcess(command, 0, output, "")


def test_detect_ready_android_device():
    controller = AndroidController(adb_path="/fake/adb", runner=FakeRunner())
    device = controller.detect()
    assert device.serial == "SERIAL"
    assert device.model == "SM-A156U"
    assert device.android_version == "16"


def test_detect_reports_unauthorized_and_missing_devices():
    unauthorized = AndroidController(
        adb_path="/fake/adb", runner=FakeRunner("SERIAL\tunauthorized")
    )
    with pytest.raises(AndroidDeviceUnauthorizedError, match="USB debugging"):
        unauthorized.detect()

    missing = AndroidController(adb_path="/fake/adb", runner=FakeRunner(""))
    with pytest.raises(AndroidDeviceNotFoundError, match="No Android phone"):
        missing.detect()


def test_one_timed_out_property_query_does_not_hide_the_phone():
    class WakingRunner(FakeRunner):
        def __call__(self, command, **kwargs):
            if command[-1:] == ["ro.product.model"]:
                raise subprocess.TimeoutExpired(command, 10)
            return super().__call__(command, **kwargs)

    devices = AndroidController(
        adb_path="/fake/adb", runner=WakingRunner()
    ).list_devices()
    assert len(devices) == 1
    assert devices[0].serial == "SERIAL"
    assert devices[0].model == "Android"


def test_set_and_clear_payloads_require_companion_exchange(monkeypatch):
    controller = AndroidController(adb_path="/fake/adb", runner=FakeRunner())
    payloads = []
    monkeypatch.setattr(controller, "_exchange", lambda payload, **kwargs: payloads.append(payload))

    controller.set_location(48.8584, 2.2945, speed=12.5)
    controller.clear_location()

    assert payloads == [
        {
            "action": "SET_LOCATION",
            "latitude": 48.8584,
            "longitude": 2.2945,
            "speed": 12.5,
        },
        {"action": "CLEAR_LOCATION"},
    ]


def test_exchange_rejects_missing_ack(monkeypatch):
    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def settimeout(self, timeout):
            pass

        def sendall(self, data):
            assert json.loads(data) == {"action": "PING"}

        def makefile(self, mode):
            class EmptyResponse:
                @staticmethod
                def readline():
                    return b""
            return EmptyResponse()

    monkeypatch.setattr("mmaps.android_device.socket.create_connection", lambda *a, **k: FakeSocket())
    controller = AndroidController(adb_path="/fake/adb", runner=FakeRunner())
    with pytest.raises(CompanionUnavailableError, match="acknowledge"):
        controller._exchange({"action": "PING"})


def test_exchange_rejects_negative_ack(monkeypatch):
    class FakeSocket:
        def __enter__(self): return self
        def __exit__(self, *args): return None
        def settimeout(self, timeout): pass
        def sendall(self, data): pass
        def makefile(self, mode):
            class Response:
                @staticmethod
                def readline(): return b'{"ok":false}\n'
            return Response()

    monkeypatch.setattr("mmaps.android_device.socket.create_connection", lambda *a, **k: FakeSocket())
    controller = AndroidController(adb_path="/fake/adb", runner=FakeRunner())
    with pytest.raises(CompanionUnavailableError, match="rejected"):
        controller._exchange({"action": "SET_LOCATION"})


def test_connect_forwards_to_separate_device_port(monkeypatch):
    runner = FakeRunner()
    controller = AndroidController(adb_path="/fake/adb", runner=runner)
    monkeypatch.setattr(controller, "companion_reachable", lambda: True)
    controller.connect()
    assert ["forward", "tcp:8766", "tcp:8765"] == runner.commands[-1][-3:]
