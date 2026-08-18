"""Small platform-neutral device records used by the unified UI.

The iPhone and Android engines stay separate because their transport and
permissions are different. This module only gives the server a common shape
for discovery and selection; it does not perform device I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from mmaps.android_device import AndroidDevice


@dataclass(frozen=True)
class DeviceCandidate:
    """A phone seen during a cheap USB/ADB discovery scan."""

    platform: str
    identifier: str
    name: str
    version: Optional[str]
    status: str = "ready"
    trusted: Optional[bool] = None
    developer_mode: Optional[bool] = None

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.identifier}"

    @property
    def connected(self) -> bool:
        return self.status in {"ready", "awaiting_confirmation"}

    def as_dict(self, *, active: bool = False) -> Dict[str, Any]:
        return {
            "key": self.key,
            "platform": self.platform,
            "identifier": self.identifier,
            "name": self.name,
            "version": self.version,
            "ios_version": self.version if self.platform == "ios" else None,
            "android_version": self.version if self.platform == "android" else None,
            "status": self.status,
            "connected": self.connected,
            "trusted": self.trusted,
            "developer_mode": self.developer_mode,
            "active": active,
        }


def ios_candidate(serial: str) -> DeviceCandidate:
    """Build a deliberately conservative iPhone record from usbmux data."""
    return DeviceCandidate(
        platform="ios",
        identifier=serial,
        name="iPhone",
        version=None,
        status="awaiting_confirmation",
        trusted=None,
        developer_mode=None,
    )


def android_candidate(device: AndroidDevice) -> DeviceCandidate:
    """Map an ADB record into the shared discovery shape."""
    status = "ready" if device.status == "device" else device.status
    return DeviceCandidate(
        platform="android",
        identifier=device.serial,
        name=device.model,
        version=device.android_version or None,
        status=status,
        trusted=device.status == "device",
        developer_mode=True if device.status == "device" else None,
    )
