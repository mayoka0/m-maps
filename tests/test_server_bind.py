"""Offline tests for LAN bind safety (no network servers started)."""
import pytest

from mmaps.server import _assert_safe_bind, is_private_lan_ip


def test_private_lan_ips():
    assert is_private_lan_ip("192.168.1.10")
    assert is_private_lan_ip("10.0.0.5")
    assert not is_private_lan_ip("8.8.8.8")
    assert not is_private_lan_ip("127.0.0.1")


def test_refuse_all_interfaces():
    with pytest.raises(ValueError):
        _assert_safe_bind("0.0.0.0", lan=False)
    with pytest.raises(ValueError):
        _assert_safe_bind("0.0.0.0", lan=True)


def test_localhost_ok_without_lan():
    _assert_safe_bind("127.0.0.1", lan=False)


def test_lan_requires_private():
    with pytest.raises(ValueError):
        _assert_safe_bind("127.0.0.1", lan=True)
    _assert_safe_bind("192.168.0.42", lan=True)
