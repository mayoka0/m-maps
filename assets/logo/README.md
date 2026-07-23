# Mayoka logo variants

Color variants of the M Maps / Mayoka mark, each as SVG (source) and PNG.

| File stem | Role |
|-----------|------|
| `mayoka-black` | **Shipping app icon** (see `assets/icon/AppIcon.icns`) |
| `mayoka-blue` | Future in-app icon picker |
| `mayoka-gold` | Future in-app icon picker |
| `mayoka-gray` | Future in-app icon picker |
| `mayoka-green` | Future in-app icon picker |
| `mayoka-orange` | Future in-app icon picker |
| `mayoka-purple` | Future in-app icon picker |
| `mayoka-red` | Future in-app icon picker |
| `mayoka-white` | Future in-app icon picker |

Rebuild the macOS icon after changing the black SVG:

```sh
venv/bin/python3 scripts/make_app_icon.py
venv/bin/python3 build_app.py
```
