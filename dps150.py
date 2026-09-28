#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyserial==3.5", "textual>=6,<9", "textual-plotext==1.0.1", "plotext>=5.2.8,<6"]
# ///
"""Checkout launcher; ordinary Python or optional `uv run` both work."""
# Keep the original import surface while routing commands through the new CLI.
import csv  # noqa: F401
import time  # noqa: F401
import serial  # noqa: F401
from dps150ctl.protocol import *  # noqa: F403
from dps150ctl.driver import DPS150, ports, select_port  # noqa: F401
from dps150ctl.trace import TraceWriter, decode_trace  # noqa: F401
from dps150ctl.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
