"""HTTP integration contract for the browser-to-Android path (no phone needed)."""
import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

if sys.platform == "win32":
    pytest.skip(
        "server imports the macOS/Unix iPhone device stack",
        allow_module_level=True,
    )

from fastapi.testclient import TestClient

from mmaps.android_device import AndroidDevice
from mmaps.android_device import AndroidDeviceNotFoundError
from mmaps import server


class FakeAndroidController:
    instances = []

    def __init__(self):
        self.locations = []
        self.clears = 0
        self.closed = 0
        self.__class__.instances.append(self)

    def connect(self):
        return AndroidDevice("SERIAL", "Samsung A15", "16")

    def list_devices(self):
        return [AndroidDevice("SERIAL", "Samsung A15", "16")]

    def set_location(self, lat, lon):
        self.locations.append((lat, lon))

    def clear_location(self):
        self.clears += 1

    def close(self):
        self.closed += 1


def test_photon_autocomplete_normalizes_geojson(monkeypatch):
    payload = {
        "features": [{
            "geometry": {"coordinates": [-83.743, 42.2808]},
            "properties": {
                "name": "Ann Arbor",
                "state": "Michigan",
                "country": "United States",
                "type": "city",
                "osm_key": "place",
            },
        }]
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(server.urllib.request, "urlopen", lambda request, timeout: Response())
    results = server._photon_search("Ann Arb", 6, 42.0, -83.0)
    assert results == [{
        "lat": 42.2808,
        "lon": -83.743,
        "name": "Ann Arbor, Michigan, United States",
        "type": "city",
        "class": "place",
    }]


def test_android_http_status_teleport_drive_fly_and_clear(monkeypatch):
    FakeAndroidController.instances.clear()
    monkeypatch.setattr(server, "AndroidController", FakeAndroidController)
    monkeypatch.setattr(server, "_DEVICE_PLATFORM", "android")
    monkeypatch.setattr(
        server.route,
        "resample_by_speed",
        lambda coordinates, speed, duration_seconds=None: [
            (coordinates[0][1], coordinates[0][0]),
            (coordinates[-1][1], coordinates[-1][0]),
        ],
    )
    monkeypatch.setattr(
        server.route,
        "resample_by_timing",
        lambda coordinates, timing, duration_seconds=None: [
            (coordinates[0][1], coordinates[0][0]),
            (coordinates[-1][1], coordinates[-1][0]),
        ],
    )
    monkeypatch.setattr(
        server.flight,
        "resample_flight",
        lambda waypoints, speed, duration_seconds=None: [
            (waypoints[0][1], waypoints[0][0]),
            (waypoints[-1][1], waypoints[-1][0]),
        ],
    )

    with TestClient(server.app) as client:
        status = client.get("/status").json()
        assert status["platform"] == "android"
        assert status["device"]["name"] == "Samsung A15"

        teleport = client.post("/spoof", json={"lat": 48.8584, "lon": 2.2945})
        assert teleport.status_code == 200
        assert teleport.json()["applied"] is True

        drive = client.post(
            "/drive",
            json={
                "coordinates": [[2.2945, 48.8584], [2.30, 48.86]],
                "mode": "car",
                "timing_sections": [{
                    "start_index": 0,
                    "end_index": 1,
                    "duration_seconds": 120,
                }],
            },
        )
        assert drive.status_code == 200
        assert drive.json()["applied"] is True
        assert drive.json()["profile_timing"] is True

        fly = client.post(
            "/fly",
            json={
                "waypoints": [[2.30, 48.86], [-0.4543, 51.47]],
                "speed": "normal",
            },
        )
        assert fly.status_code == 200
        assert fly.json()["applied"] is True

        assert client.post("/stop_move").status_code == 200
        assert client.post("/stop").status_code == 200

    controller = FakeAndroidController.instances[0]
    assert controller.locations
    assert controller.clears == 1
    assert controller.closed == 1


def test_android_can_attach_after_server_started(monkeypatch):
    class LateController(FakeAndroidController):
        attempts = 0

        def connect(self):
            self.__class__.attempts += 1
            if self.__class__.attempts == 1:
                raise AndroidDeviceNotFoundError("not plugged in yet")
            return super().connect()

    async def scenario():
        from fastapi import FastAPI

        test_app = FastAPI()
        test_app.state.session = None
        test_app.state.startup_error = None
        test_app.state.confirmed_udid = None
        controller = LateController()
        with pytest.raises(AndroidDeviceNotFoundError):
            await server._attach_android_session(test_app, controller)
        assert test_app.state.session is None
        await server._attach_android_session(test_app, controller)
        assert test_app.state.session is not None
        assert test_app.state.startup_error is None

    # The poll retries this helper after an initial startup failure. Exercise
    # the successful retry directly without adding a one-second sleep to tests.
    LateController.attempts = 0
    asyncio.run(scenario())


def test_auto_mode_lists_both_platforms_and_selects_android(monkeypatch):
    class DiscoveryController(FakeAndroidController):
        def list_devices(self):
            return [AndroidDevice("SERIAL", "Samsung A15", "16")]

        def connect(self, serial=None):
            assert serial in (None, "SERIAL")
            return AndroidDevice("SERIAL", "Samsung A15", "16")

    async def ios_serials():
        return ["IPHONE-SERIAL"]

    monkeypatch.setattr(server, "AndroidController", DiscoveryController)
    monkeypatch.setattr(server, "_DEVICE_PLATFORM", "auto")
    monkeypatch.setattr(server, "_usb_serials", ios_serials)

    with TestClient(server.app) as client:
        status = client.get("/status").json()
        keys = {item["key"] for item in status["devices"]}
        assert keys == {"ios:IPHONE-SERIAL", "android:SERIAL"}
        assert status["active_device"] is None

        selected = client.post(
            "/device/select",
            json={"platform": "android", "identifier": "SERIAL"},
        )
        assert selected.status_code == 200
        assert selected.json()["active"] == "android:SERIAL"


def test_auto_mode_requires_one_phone_when_both_are_connected(monkeypatch):
    class DiscoveryController(FakeAndroidController):
        def list_devices(self):
            return [AndroidDevice("SERIAL", "Samsung A15", "16")]

    async def ios_serials():
        return ["IPHONE-SERIAL"]

    monkeypatch.setattr(server, "AndroidController", DiscoveryController)
    monkeypatch.setattr(server, "_DEVICE_PLATFORM", "auto")
    monkeypatch.setattr(server, "_usb_serials", ios_serials)

    with TestClient(server.app) as client:
        status = client.get("/status").json()
        assert status["state"] == "device_action_required"
        assert status["active_device"] is None
        assert "Unplug one phone" in status["error"]


def test_absent_active_iphone_is_reported_disconnected_immediately():
    test_app = SimpleNamespace(
        state=SimpleNamespace(
            active_platform="ios",
            active_device_key="ios:IPHONE-SERIAL",
            discovered_devices={},
        )
    )
    result = {
        "device": {"name": "iPhone", "connected": True, "link_lost": False},
        "state": "ready",
        "applied": True,
        "assert_live": True,
    }

    server._apply_physical_presence(test_app, result)

    assert result["device"]["connected"] is False
    assert result["device"]["link_lost"] is True
    assert result["state"] == "reconnecting"
    assert result["applied"] is False
    assert result["assert_live"] is False


def test_android_auto_attaches_after_active_iphone_is_unplugged(monkeypatch):
    class UnlockedSessionLock:
        @staticmethod
        def locked():
            return False

    class OldIphoneSession:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    old_session = OldIphoneSession()
    attached = []

    async def no_iphones():
        return []

    async def android_candidates(_app):
        return [server.android_candidate(AndroidDevice("SERIAL", "Samsung A15", "16"))]

    async def attach_android(app, _controller, serial=None):
        attached.append(serial)
        app.state.session = "android-session"
        app.state.active_platform = "android"
        app.state.active_device_key = f"android:{serial}"
        return app.state.session

    test_app = SimpleNamespace(
        state=SimpleNamespace(
            session=old_session,
            session_lock=UnlockedSessionLock(),
            active_platform="ios",
            active_device_key="ios:IPHONE-SERIAL",
            confirmed_udid="IPHONE-SERIAL",
            discovered_devices={},
            device_message=None,
            pending_device=None,
            ignored_udids=set(),
            android_controller=object(),
        )
    )
    monkeypatch.setattr(server, "_DEVICE_PLATFORM", "auto")
    monkeypatch.setattr(server, "_usb_serials", no_iphones)
    monkeypatch.setattr(server, "_refresh_android_candidates", android_candidates)
    monkeypatch.setattr(server, "_attach_android_session", attach_android)

    asyncio.run(server._device_poll_once(test_app))

    assert old_session.closed is True
    assert attached == ["SERIAL"]
    assert test_app.state.active_device_key == "android:SERIAL"


def test_auto_mode_selecting_iphone_keeps_explicit_confirmation(monkeypatch):
    class DiscoveryController(FakeAndroidController):
        def list_devices(self):
            return [AndroidDevice("SERIAL", "Samsung A15", "16")]

    async def ios_serials():
        return ["IPHONE-SERIAL"]

    monkeypatch.setattr(server, "AndroidController", DiscoveryController)
    monkeypatch.setattr(server, "_DEVICE_PLATFORM", "auto")
    monkeypatch.setattr(server, "_usb_serials", ios_serials)

    with TestClient(server.app) as client:
        selected = client.post(
            "/device/select",
            json={"platform": "ios", "identifier": "IPHONE-SERIAL"},
        )
        assert selected.status_code == 200
        assert selected.json()["requires_confirmation"] is True
        assert selected.json()["pending_device"]["udid"] == "IPHONE-SERIAL"
