# Contributing to M Maps

Thanks for wanting to help. This is a small open-source macOS tool — keep
changes focused, readable, and tested where you can.

## Getting set up

You'll need:

- macOS on Apple Silicon
- Xcode Command Line Tools (`xcode-select --install`)
- An iPhone on iOS 17+ (only if you want to test against a real device)

```sh
git clone https://github.com/mayoka0/m-maps.git
cd m-maps
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

**Desktop app** (recommended for day-to-day work):

```sh
venv/bin/python3 m_maps.py app
```

That opens a native window and will ask for your Mac password once — the map
server needs admin rights to create the USB tunnel (`utun`). You can also build
a double-clickable app:

```sh
venv/bin/python3 build_app.py    # → dist/M Maps.app
venv/bin/python3 build_dmg.py    # → dist/M Maps.dmg (optional)
```

**Browser UI** instead of the desktop window:

```sh
sudo venv/bin/python3 m_maps.py serve
```

Always use the venv's Python under `sudo` (don't rely on an activated shell
PATH). Plug in the phone, unlock it, and approve Trust / Developer Mode if
prompted.

More usage notes live in the [README](README.md).

## How we take contributions

1. Open an issue first if the change is large or unclear — saves everyone time.
2. Fork (or branch from `main`), do the work, and open a **pull request**.
3. PRs need a clear description and a review/approval before merging to `main`.
4. Keep the PR scoped: one idea or fix per PR is easier to review than a kitchen
   sink.

Please be kind in reviews and discussions — see [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## What a good PR looks like

- **What & why** — a few sentences on the problem and the approach.
- **Tested** — say what you ran. CI (when available) can exercise pure Python
  logic and basic packaging, but **it cannot talk to a real iPhone**. If your
  change touches location, the hold loop, USB/reconnect, or the map controls
  that drive the phone, please test on a real device when you can and note that
  in the PR.
- **No drive-by refactors** unless they're needed for the fix.
- **Readable code** — this project is open source so people can see exactly what
  runs on their phone. Prefer clear names and short comments over cleverness.

## Reporting bugs and ideas

- Bugs and features: open a GitHub issue (templates are there to guide you).
- **Security** problems: please don't file a public issue — see
  [SECURITY.md](SECURITY.md).

## Questions

Open an issue or start a discussion on the repo. Happy to point you at the right
file if you're not sure where something lives.
