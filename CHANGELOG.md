# Changelog

All notable changes to M Maps are listed here. Version numbers match GitHub release tags.

## [1.0.2] — 2026-07-23

- Fixed the menu-bar app name so it shows **M Maps** (not “Python”), by root-causing how pywebview builds the Cocoa application menu.
- Regenerated the shipping app icon as a padded white squircle (macOS-style margin and continuous corners), instead of a hard-edged full-bleed square.
- Tightened the DMG installer window (compact size, branded background, drag-to-Applications cue).

### Known issue (as of 1.0.2)

- The **Dock hover tooltip** and **app switcher (Cmd+Tab)** can still show **Python** in some cases. That is a separate Launch Services / process-bundle identity issue from the menu-bar fix, and is still being worked on.

## [1.0.1] — 2026-07-23

- Attempted fix for the app appearing as “Python” instead of “M Maps” in the menu bar (in-process name / bundle-string patches). **That identity fix did not fully work** in real installs.
- Added a notify-only update check (GitHub Releases API; no auto-install).

## [1.0.0] — 2026-07-23

- First public release.
- Native macOS app and browser map UI over a USB spoof session.
- Core modes: Teleport, Route (walk / bicycle / motorcycle / car), Fly, Multi-stop.
- MIT license; open-source packaging (self-contained `.app` / `.dmg` build).
