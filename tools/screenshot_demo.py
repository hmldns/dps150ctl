"""Generate the README screenshot entirely from synthetic data.

Run after installing the package: python tools/screenshot_demo.py
"""
import asyncio
import math
import os
from pathlib import Path
import tempfile
import time

from dps150ctl.demo import DemoDevice
from dps150ctl.identity import Identity
from dps150ctl.ipc import Client, Endpoint, Service
from dps150ctl.tui import Dashboard
from dps150ctl.worker import DeviceWorker


async def screenshot():
    # Documentation is intentionally in color; normal CLI runs respect NO_COLOR.
    os.environ.pop("NO_COLOR", None)
    with tempfile.TemporaryDirectory(prefix="dps-shot-") as root:
        identity = Identity(None, "demo")
        endpoint = Endpoint(identity, Path(root))
        owner = Service(DeviceWorker(identity, factory=DemoDevice, reconnect=False), endpoint).start()
        try:
            app = Dashboard(Client(endpoint), demo=True, owns_service=True)
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause(0.5)
                now = time.monotonic()
                app.history.samples.clear()
                # Illustrative demo history, never presented as physical measurements.
                for index in range(241):
                    elapsed = index / 2
                    voltage = 3.3 + 0.008 * math.sin(elapsed * 1.8)
                    current = 0.22 + 0.09 * math.sin(elapsed * 0.7)
                    app.history.samples.append((now - 120 + elapsed, voltage, current, 1))
                app.draw()
                await pilot.pause(0.1)
                destination = Path(__file__).resolve().parents[1] / "docs"
                destination.mkdir(exist_ok=True)
                print(app.save_screenshot("dashboard.svg", path=str(destination)))
        finally:
            owner.close()


if __name__ == "__main__":
    asyncio.run(screenshot())
