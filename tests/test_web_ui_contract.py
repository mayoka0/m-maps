"""Static contracts for safety-critical map UI behavior."""
from pathlib import Path


WEB_DIR = Path(__file__).resolve().parents[1] / "mmaps" / "web"


def test_maplibre_disables_repeated_worlds():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert "renderWorldCopies: false" in html
    # A [-180, 180] maxBounds combined with disabled world copies can clamp
    # MapLibre onto the antimeridian and leave a split blank canvas.
    assert "maxBounds: [[-180" not in html
    assert "Math.max(0, Math.min(1" in html
    assert "Antipodal endpoints have no unique great circle" in html


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


def test_sidebar_and_safety_controls_stack_above_map_content():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    header_start = html.index("header {")
    header_end = html.index("}", header_start)
    assert "z-index: 110" in html[header_start:header_end]
    assert "z-index: 109" in html


def test_device_confirmation_is_a_foreground_modal():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    prompt_start = html.index("#devicePrompt {")
    prompt_end = html.index("}", prompt_start)
    prompt_css = html[prompt_start:prompt_end]
    assert "position: fixed" in prompt_css
    assert "z-index: 1000" in prompt_css
    assert 'role="dialog"' in html
    assert 'aria-modal="true"' in html
    assert "prompt.style.display = 'flex'" in html


def test_unified_device_choice_is_automatic_in_the_ui():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="devicePicker"' not in html
    assert "function renderDevicePicker" not in html
    assert "postJSON('/device/select'" not in html
    assert "device_action_required" in html
    assert "Plug in an iPhone or Android phone" in html


def test_sidebar_search_is_persistent_with_debounced_autocomplete():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="searchToggle"' not in html
    assert 'id="searchBtn"' not in html
    assert "function setSearchOpen" not in html
    assert 'class="searchGlyph"' in html
    assert 'placeholder="Search places"' in html
    assert "scheduleLiveSearch" not in html
    assert "function autocompletePlaces" in html
    assert "fetch('/autocomplete?'" in html
    assert "}, 400);" in html
    assert "renderPlaceResults(out.results" in html


def test_routes_use_distinct_access_aware_profiles_and_router_timing():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert "https://valhalla1.openstreetmap.de/route" in html
    assert "https://router.project-osrm.org/route/v1/driving/" in html
    assert "if (useMode === 'car')" in html
    assert "costing: 'pedestrian'" in html
    assert "costing: 'bicycle'" in html
    assert "use_roads: 0.2" in html
    assert "costing: 'motorcycle'" in html
    assert "costing: 'auto'" in html
    assert "timing_sections: timing" in html
    assert "body.timing_sections = timingSections" in html


def test_walk_has_a_shared_long_route_guard():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert "const MAX_WALK_ROAD_KM = 15" in html
    assert "Walk is available only up to ${MAX_WALK_ROAD_KM} km" in html


def test_header_stays_minimal_and_journey_controls_live_in_sidebar():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    header = html[html.index("<header>"):html.index("</header>")]
    sidebar_start = html.index('<aside id="settingsPanel"')
    sidebar_end = html.index("</aside>", sidebar_start)
    sidebar = html[sidebar_start:sidebar_end]
    assert 'id="sidebarBtn"' in header
    assert 'id="stopBtn"' in header
    assert 'id="controls"' not in header
    assert 'id="settingsBtn"' not in html
    assert 'class="brand' not in header
    assert 'id="deviceSummary"' not in header
    assert 'id="controls"' in sidebar
    assert 'id="searchInput"' in sidebar
    assert 'id="modeSeg"' in sidebar
    assert 'id="speedSeg"' in sidebar
    assert 'id="tripGroup"' in sidebar


def test_device_status_lives_in_sidebar_not_the_primary_controls():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    sidebar_start = html.index('<aside id="settingsPanel"')
    sidebar = html[sidebar_start:html.index("</aside>", sidebar_start)]
    assert 'id="settingsDeviceTitle"' in sidebar
    assert 'id="deviceSummary"' in sidebar
    assert 'id="deviceDetails" class="open"' in sidebar
    assert 'id="deviceName"' in sidebar


def test_sidebar_preserves_dark_glass_material_and_reduced_motion_fallback():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert "background: rgba(22, 27, 38, 0.62)" in html
    assert "--text: #f2f5f9" in html
    assert "width: min(260px, calc(100vw - 20px))" in html
    assert "width: 28px" in html
    assert "transform: translateX(calc(-100% - 28px))" in html
    assert "body.sidebar-open #sidebarBtn" in html
    assert "@media (prefers-reduced-motion: reduce)" in html


def test_native_sidebar_header_shares_the_traffic_light_row():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert "html.native-window #settingsPanel .sidebarHeader" in html
    assert "top: var(--safe-top)" in html
    assert "left: var(--safe-left)" in html
    assert "padding-left: 76px" in html
    assert "padding-top: 0" in html
    assert "justify-content: flex-end" in html
    sidebar_start = html.index('<aside id="settingsPanel"')
    sidebar = html[sidebar_start:html.index("</aside>", sidebar_start)]
    assert '<div class="settingsTitle">M Maps</div>' not in sidebar
    assert 'id="journeyTitle"' not in sidebar


def test_sidebar_journey_wrapper_does_not_stack_a_second_glass_surface():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    start = html.rindex("#settingsPanel #controls {")
    end = html.index("}", start)
    controls_css = html[start:end]
    assert "background: transparent" in controls_css
    assert "box-shadow: none" in controls_css
    assert "-webkit-backdrop-filter: none" in controls_css
    assert "backdrop-filter: none" in controls_css


def test_sidebar_has_accessible_open_close_behavior():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert 'aria-controls="settingsPanel" aria-expanded="false"' in html
    assert 'aria-label="M Maps controls" aria-modal="false"' in html
    assert "function openSidebar()" in html
    assert "function closeSidebar(options)" in html
    assert "e.key === 'Escape' && isSidebarOpen()" in html
    assert "button.setAttribute('aria-expanded', 'true')" in html


def test_destination_confirmations_have_keyboard_and_screen_reader_behavior():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="findPanel" class="floatCenter" role="dialog" aria-modal="true"' in html
    assert 'aria-labelledby="findTitle" tabindex="-1"' in html
    assert 'id="goPanel" class="floatCenter" role="dialog" aria-modal="true"' in html
    assert 'aria-labelledby="goTitle" tabindex="-1"' in html
    assert 'id="findTitle" aria-live="polite"' in html
    assert 'id="goTitle" aria-live="polite"' in html
    assert "panel.__mmapsReturnFocus" in html
    assert "const first = Array.from(panel.querySelectorAll(" in html
    assert "Confirmation panels are modal for keyboard navigation" in html
    assert "if (e.key === 'Escape')" in html
    assert "if (e.shiftKey && document.activeElement === first)" in html


def test_route_confirmation_is_visible_while_routing_and_times_out_cleanly():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert "Finding a ${escapeHtml" in html
    assert "controller.abort(), 12000" in html
    assert "error === 'timeout'" in html
    assert "loading: true" in html
    assert "setFloatPanel('goPanel', true);" in html
    assert "road = await fetchRoadRoute(from, dest, speedKey)" in html
    assert "road = { error: 'network', detail: null }" in html
    assert "return { error: 'bad_response' }" in html


def test_sidebar_close_control_is_a_minimal_back_arrow():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="settingsCloseBtn"' in html
    assert 'aria-label="Close M Maps controls"' in html
    assert 'd="m14.5 6-6 6 6 6"' in html
    assert "position: absolute" in html
    assert "border-color: transparent" in html
    assert "background: transparent" in html
    assert "body.sidebar-open #sidebarBtn" in html


def test_bottom_status_banner_uses_dark_text_on_solid_tints():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert "background: rgba(248, 205, 92, 0.90)" in html
    assert "color: #17130a" in html
    assert "background: rgba(255, 163, 169, 0.90)" in html
    assert "color: #21090c" in html


def test_native_window_reclaims_titlebar_without_covering_traffic_lights():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert "document.documentElement.classList.add('native-window')" in html
    assert "html.native-window header" in html
    assert "top: calc(2px + var(--safe-top))" in html
    assert "calc(84px + var(--safe-left))" in html
    assert "border-radius: 0" in html
    assert "background: transparent" in html
    assert "box-shadow: none" in html
    assert 'class="brand pywebview-drag-region"' not in html
    assert 'class="spacer pywebview-drag-region"' in html


def test_status_notices_are_visible_at_bottom_and_float_cards_make_room():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="noticeStack"' in html
    assert "#noticeStack {" in html
    assert "bottom: calc(14px + var(--safe-bottom))" in html
    assert "--notice-stack-space: 0px" in html
    assert "function syncNoticeLayout()" in html
    assert "var(--notice-stack-space)" in html


def test_map_has_no_persistent_instruction_pill():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert 'id="hint"' not in html
    assert "Approx location:" not in html
    assert "const hint = $('hint')" not in html


def test_trip_status_poll_does_not_replace_open_wait_selector():
    html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    assert "let tripListRenderKey = null" in html
    assert "if (nextRenderKey !== tripListRenderKey)" in html
    assert "tripListRenderKey = makeTripListRenderKey(true)" in html
