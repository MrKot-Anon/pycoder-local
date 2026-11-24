"""Standalone coding agent with an optional Textual frontend."""

from pathlib import Path
import sys

# Source checkout uses explicitly downloaded build dependencies. Bundles contain
# these UI packages at the zip root. Native E2EE dependencies are installed only
# by the explicit --setup-e2ee command into a separate user environment.
_vendor = Path(__file__).resolve().parent.parent / "vendor"
if _vendor.is_dir():
    sys.path.insert(0, str(_vendor))

__version__ = "0.6.5"
