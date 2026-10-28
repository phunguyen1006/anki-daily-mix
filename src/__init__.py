# Copyright: Anki Daily Mix contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""Implementation package for the Anki Daily Mix add-on.

The add-on entry point lives in the repository root (``__init__.py``).  Keeping
the implementation in ``src`` makes the pure-Python selection engine importable
by the test suite without booting Anki.
"""

from __future__ import annotations

__all__ = ["config", "constants", "engine", "ui_dashboard", "ui_settings"]
