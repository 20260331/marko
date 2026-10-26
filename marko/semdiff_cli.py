"""Command line entry point for the semantic diff.

Usage::

    python -m marko.semdiff_cli old.md new.md
"""

from __future__ import annotations

from .semdiff import main

if __name__ == "__main__":
    raise SystemExit(main())
