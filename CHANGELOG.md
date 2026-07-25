# Changelog

All notable changes to M Maps are listed here. Version numbers match GitHub release tags.
Unreleased develop builds use `X.Y.Z-beta.N` (not tagged as public releases).

## [Unreleased] — 1.0.4-beta.1 (develop only)

### Realistic motion

- Road speeds recalibrated to real-world averages: walk 5, bicycle 16, motorcycle 55, car **60** km/h (was car 90). Duration = path distance ÷ speed — no artificial speedup.
- Flight uses real commercial cruise (~**875** km/h) with mild slow/normal/fast (±15%). Removed the old 60–240× time-compression so long-haul hops take real hours (e.g. Nairobi→NYC ~13–15 h).
- Drive tick 0.25 s + lower lateral jitter for fluid Find My motion at the slower pace; fly tick 0.5 s.

### Beta channel

- Develop builds identify as `1.0.4-beta.N` with an amber version chip (“pre-release / develop build”). Not for public GitHub Releases.

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
