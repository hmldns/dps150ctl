"""Every test is isolated from user configuration, services and real hardware."""
import os
from pathlib import Path
import tempfile

import pytest
import serial


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="dps-test-") as root:
        monkeypatch.setenv("XDG_RUNTIME_DIR", root)
        monkeypatch.setenv("XDG_CONFIG_HOME", root)
        monkeypatch.delenv("DPS150_PORT", raising=False)
        original = serial.Serial
        def guarded_serial(*args, **kwargs):
            port = kwargs.get("port", args[0] if args else "")
            if not os.path.realpath(port).startswith("/dev/pts/"):
                raise AssertionError(f"tests must never open a real device: {port}")
            return original(*args, **kwargs)
        monkeypatch.setattr(serial, "Serial", guarded_serial)
        yield Path(root)
