"""M Maps — spoofs the location an iPhone reports to macOS over USB, for harmless pranks."""

from typing import Optional

# Public releases: "1.0.3" (matches GitHub tags vX.Y.Z).
# Local / develop beta builds: "1.0.4-beta.N" — never tag these as public releases.
# Bump the beta N on develop as you iterate; promote to "1.0.4" only when cutting main.
__version__ = "1.0.4-beta.2"


def is_beta_version(version: Optional[str] = None) -> bool:
    """True for pre-release strings like ``1.0.4-beta.1`` (not a stable public tag)."""
    v = (version if version is not None else __version__).strip().lower()
    return "beta" in v or "rc" in v or "alpha" in v or "dev" in v
