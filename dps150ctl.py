#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyserial==3.5", "textual>=6,<9", "textual-plotext==1.0.1", "plotext>=5.2.8,<6"]
# ///
"""Checkout launcher; ordinary Python or optional `uv run` both work."""
from dps150ctl.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
