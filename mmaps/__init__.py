"""M Maps — spoofs the location an iPhone reports to macOS over USB, for harmless pranks."""

from typing import Optional

# Public releases use clean X.Y.Z versions matching GitHub tags.
# Develop builds may use X.Y.Z-beta.N, but only when MJ explicitly chooses a
# version checkpoint. 1.0.5 ships the foreground device-confirmation fix.
__version__ = "1.0.5"


def is_beta_version(version: Optional[str] = None) -> bool:
    """True for pre-release strings like ``1.0.5-beta.1`` (not a stable public tag)."""
    v = (version if version is not None else __version__).strip().lower()
    return "beta" in v or "rc" in v or "alpha" in v or "dev" in v
