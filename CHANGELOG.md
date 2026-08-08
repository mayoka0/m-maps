# Changelog

All notable changes to M Maps are listed here. Version numbers match GitHub release tags.
Unreleased develop builds use `X.Y.Z-beta.N` (not tagged as public releases).

## [1.0.4] — 2026-08-07

### Road fidelity and movement

- Road playback now follows the complete OSRM geometry exactly by default. Unconstrained
  Catmull–Rom smoothing is legacy opt-in only because it could cut a junction, briefly enter a
  nearby road, and then double back.
- Removed synthetic lateral jitter so generated road points stay on the router's centerline.
- **Cosine velocity easing** still ramps from rest, eases into the destination, and slows through
  sharp turns without changing the routed geometry. Fly uses takeoff/landing ramps.
- Protocol still only sets lat/lon — naturalness comes entirely from the point stream and timing.

### Map and interface

- Added optional Google Maps support with a bring-your-own API key stored in browser localStorage;
  OpenFreeMap remains the free no-key default.
- Added provider-neutral overlays and hardened provider switching/retry behavior.
- Redesigned the controls as one translucent command bar with compact device details, collapsed
  explicit search, settings, and clearer visual hierarchy.
- Teleport map clicks now require confirmation, preventing an accidental click from moving the
  phone immediately.
- Disabled repeated MapLibre worlds and added viewport-aware Google minimum zoom/bounds.
- Reduced Google zoom lag by removing expensive backdrop blur while Google tiles animate.

### Multi-stop waits

- Stops can hold for a preset or custom 1–1,440 minute dwell before the next leg.
- Added a live wait countdown and **Leave now** action; the final stop continues to hold normally.
- Kept wait selectors stable across one-second status polling so their menus no longer disappear
  while the user is choosing a duration.

### Realistic speeds (from beta.1)

- Road speeds: walk 5, bicycle 16, motorcycle 55, car **60** km/h. Duration ≈ path distance ÷ speed.
- Flight cruise ~**875** km/h (mild slow/normal/fast ±15%); no 60–240× compression.
- Drive tick 0.25 s with no lateral jitter; fly tick 0.5 s, no jitter.

## [1.0.3] — 2026-07-24

### Packaging & identity

- Rebuilt the desktop app with **PyInstaller** (windowed bootloader): Dock / Cmd+Tab stay **M Maps** for the full launch bounce (no nested Python.app process identity).
- Split elevated tunnel work into a console helper (`mmaps-server`) so the GUI process no longer freezes / “Not Responding” when the password dialog starts the root server.
- Fixed Info.plist so the app is a normal Dock citizen (`LSBackgroundOnly=false`) and the system can style the icon with appearance / tint.
- Icon rebuilt from Apple’s official App Icon Template measurements + Dock presentation padding.

### Motion & UI

- Route / drive motion updates **5× per second** (was once per second) so the Find My dot glides instead of stop–go.
- Visual polish: glass-style translucent panels (WebKit `backdrop-filter`), refreshed indigo/slate palette, clearer update-available banner.

### Reliability

- Desktop attach: Trust dialog works headless; clear message when Developer Mode is off; attach the **confirmed** USB serial when multiple phones are present.
- Clearing “link lost” after Stop & restore GPS so status chips recover correctly.

## [1.0.2] — 2026-07-23

- Fixed the menu-bar app name so it shows **M Maps** (not “Python”), by root-causing how pywebview builds the Cocoa application menu.
- Regenerated the shipping app icon as a padded white squircle (macOS-style margin and continuous corners), instead of a hard-edged full-bleed square.
- Tightened the DMG installer window (compact size, branded background, drag-to-Applications cue).

### Known issue (as of 1.0.2 — fixed in 1.0.3)

- Dock hover / Cmd+Tab showing **Python** was addressed in 1.0.3 via the PyInstaller bootloader packaging.

## [1.0.1] — 2026-07-23

- Attempted fix for the app appearing as “Python” instead of “M Maps” in the menu bar (in-process name / bundle-string patches). **That identity fix did not fully work** in real installs.
- Added a notify-only update check (GitHub Releases API; no auto-install).

## [1.0.0] — 2026-07-23

- First public release.
- Native macOS app and browser map UI over a USB spoof session.
- Core modes: Teleport, Route (walk / bicycle / motorcycle / car), Fly, Multi-stop.
- MIT license; open-source packaging (self-contained `.app` / `.dmg` build).
