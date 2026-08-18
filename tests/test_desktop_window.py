"""Static contracts for the native macOS window shell."""
from pathlib import Path


DESKTOP = Path(__file__).resolve().parents[1] / "mmaps" / "desktop.py"


def test_native_window_uses_full_size_content_and_restores_native_chrome():
    source = DESKTOP.read_text(encoding="utf-8")
    assert "frameless=True" in source
    assert "easy_drag=False" in source
    assert "setTitlebarAppearsTransparent_(True)" in source
    assert "setTitleVisibility_(NSWindowTitleHidden)" in source
    assert "standardWindowButton_(button_type)" in source
    assert "button.setHidden_(False)" in source
    assert 'desktop_url = url + ("&" if "?" in url else "?") + "desktop=1"' in source
