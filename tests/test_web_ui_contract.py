"""Static contracts for safety-critical map UI behavior."""
from pathlib import Path


WEB_DIR = Path(__file__).resolve().parents[1] / "mmaps" / "web"


def test_maplibre_disables_repeated_worlds():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert "renderWorldCopies: false" in html
    # A [-180, 180] maxBounds combined with disabled world copies can clamp
    # MapLibre onto the antimeridian and leave a split blank canvas.
    assert "maxBounds: [[-180" not in html


def test_google_map_is_restricted_to_one_world():
    source = (WEB_DIR / "js" / "mmaps-map-provider.js").read_text(encoding="utf-8")
    assert "strictBounds: true" in source
    assert "west: -180, east: 180" in source
    assert "function minimumWorldZoom" in source
    assert "Math.ceil(Math.log(width / 256) / Math.LN2)" in source
    assert "zoom = Math.max(minWorldZoom" in source


def test_teleport_map_click_uses_confirmation_not_direct_spoof():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    start = html.index("async function handleMapClick")
    end = html.index("function mapFlyTo", start)
    handler = html[start:end]
    assert "showFoundPlace(lat, lon, null)" in handler
    assert "await teleport(lat, lon)" not in handler


def test_header_stacks_above_controls_for_device_popover():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    header_start = html.index("header {")
    header_end = html.index("}", header_start)
    controls_start = html.index("#controls {")
    controls_end = html.index("}", controls_start)
    assert "z-index: 110" in html[header_start:header_end]
    assert "z-index: 100" in html[controls_start:controls_end]


def test_search_is_collapsed_and_explicit_submit_only():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="searchToggle"' in html
    assert "function setSearchOpen" in html
    assert "scheduleLiveSearch" not in html
    assert "$('searchInput').addEventListener('input'" not in html


def test_controls_live_inside_the_floating_header():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    header = html[html.index("<header>"):html.index("</header>")]
    assert 'id="controls"' in header
    assert 'id="settingsBtn"' in header
    assert 'id="stopBtn"' in header


def test_trip_status_poll_does_not_replace_open_wait_selector():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert "let tripListRenderKey = null" in html
    assert "if (nextRenderKey !== tripListRenderKey)" in html
    assert "tripListRenderKey = makeTripListRenderKey(true)" in html
