# Contributing to M Maps

Thanks for wanting to help. This is a small open-source macOS tool - keep
changes focused, readable, and tested where you can.

## Getting set up

You'll need:

- macOS on Apple Silicon
- Xcode Command Line Tools (`xcode-select --install`)
- An iPhone on iOS 17+ (only if you want to test against a real device)
- Android Studio plus an Android phone with USB debugging (only for Android work)

```sh
git clone https://github.com/mayoka0/m-maps.git
cd m-maps
python3 -m venv venv
venv/bin/pip install -r requirements.txt
# For running the offline test suite:
venv/bin/pip install -r requirements-dev.txt
```

**Desktop app** (recommended for day-to-day work):

```sh
venv/bin/python3 m_maps.py app
```

That opens a native window and will ask for your Mac password once - the map
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

`main` is protected: **no direct pushes**, **no force-push**, **no branch
deletion**. Everything lands via a pull request.

### Branch layout

Three long-lived branches during cross-platform development:

| Branch | Role |
|--------|------|
| `main` | Protected release line; tags (`vX.Y.Z`) are cut from here. |
| `develop` | Day-to-day work; open PRs land here first. |
| `cross-platform-develop` | Unified iPhone/Android beta work; cross-platform PRs land here first. |

Feature work uses short-lived branches (e.g. `fix/…`) that are **deleted after
merge**. macOS/iPhone-only work branches from and targets `develop`; unified iPhone/Android work
branches from and targets `cross-platform-develop`. Stable releases are integrated into `main`
only by the maintainer.

### Version numbers

- **Stable / public:** `mmaps/__init__.py` → `__version__ = "X.Y.Z"`, tagged on
  `main` as `vX.Y.Z` (GitHub Release).
- **Develop / local beta:** `__version__ = "X.Y.Z-beta.N"` (e.g. `1.0.4-beta.1`).
  The app’s version chip turns amber and is labeled as a pre-release. **Never**
  tag beta strings as public GitHub Releases - promote by setting a clean
  `X.Y.Z` when cutting `main`.

### Maintainer workflow (solo / agent-assisted) - nothing auto-ships

For day-to-day work on this machine (including AI coding agents):

1. **Build and test locally.** Do **not** commit or push to **any** branch
   (including `develop`) without **explicit** maintainer permission **each
   time**. Finishing a coding task is not permission to ship.
2. When a change set is ready, **ask** whether it is good to push to `develop`
   - then wait. Do not push as an automatic “task complete” step.
3. **Do not bump version numbers (including beta `N`) on every change.** Bump
   only when the maintainer calls a meaningful checkpoint. Related changes
   should accumulate and be tested together under one version.
4. **Public releases** (`main`, tags, GitHub Releases) stay equally strict -
   only when the maintainer explicitly asks.

Slower, deliberate, maintainer-confirmed steps beat fast automatic pushes.

### Solo-maintainer review policy (temporary)

While Mayoka Labs is effectively a **solo maintainer**, the “require an
approving review” rule on `main` is **relaxed to 0 required approvals** so the
maintainer can merge their own PRs without a second GitHub account. **Other
protections stay on** (no force-push to `main`, no deleting `main`).

**When outside contributors regularly open PRs, re-enable at least 1 required
approving review** on `main` (Settings → Branches → Branch protection). Do not
forget this - dual review is the right default for a multi-person project.

1. Open an issue first if the change is large or unclear - saves everyone time.
2. Fork (or branch from `develop`), do the work, and open a **pull request**.
3. Keep the PR scoped: one idea or fix per PR is easier to review than a kitchen
   sink.
4. Be kind in reviews and discussions - see [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).

## Automated checks (CI)

Every pull request runs **GitHub Actions** (see `.github/workflows/ci.yml`):

- **pytest** offline unit tests for pure Python helpers (`tests/`)
- Matrix: **macOS / Ubuntu / Windows** × **Python 3.12-3.14**
- **Coverage** upload (Codecov) on the Ubuntu 3.12 job

```sh
venv/bin/pip install -r requirements-dev.txt
venv/bin/python3 -m pytest tests/ -v
```

### Hard limit: CI cannot replace a real phone

Automated CI verifies pure logic (routing math, bind safety, error strings,
etc.). **It cannot open an iPhone tunnel, authorize ADB, run the Android companion, or verify real
GPS.** Changes that
touch location, the hold loop, USB/reconnect, elevation, or map controls that
drive the phone still need a **human on-device check** (typically the
maintainer) before merge. Say what you ran in the PR description.

## Issue labels

Useful labels when filing or hunting work:

| Label | Meaning |
|-------|---------|
| `good first issue` | Friendly for newcomers |
| `help wanted` | Maintainer wants outside help |
| `difficulty: easy` / `medium` / `hard` | Rough effort estimate |
| `bug` | Something is broken |
| `enhancement` | New feature or improvement |
| `documentation` | Docs only |

## What a good PR looks like

- **What & why** - a few sentences on the problem and the approach.
- **Tested** - note CI + any local `pytest` run; note on-device testing when
  relevant.
- **No drive-by refactors** unless they're needed for the fix.
- **Readable code** - this project is open source so people can see exactly what
  runs on their phone. Prefer clear names and short comments over cleverness.

## Reporting bugs and ideas

- Bugs and features: open a GitHub issue (templates are there to guide you).
- **Security** problems: please don't file a public issue - see
  [SECURITY.md](SECURITY.md).

## Questions

Open an issue or start a discussion on the repo. Happy to point you at the right
file if you're not sure where something lives.
