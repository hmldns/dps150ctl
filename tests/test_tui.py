import asyncio
import time

import pytest
from textual.widgets import Digits, Input

from dps150ctl.state import StateStore
from dps150ctl.identity import Identity
from dps150ctl.tui import Dashboard, History, SetpointScreen
pytest_plugins = ["test_service"]


def test_dashboard_controls_views_and_resize(service):
    owner, client = service
    async def scenario():
        app = Dashboard(client, demo=True, max_voltage=1.0)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            assert app.fresh()
            assert app.query_one("#voltage", Digits).value != "--.---"
            await pilot.press("v")
            assert isinstance(app.screen, SetpointScreen)
            field = app.screen.query_one(Input)
            field.value = "1.01"
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, SetpointScreen)
            assert owner.worker.device.write_attempts == 0
            field.value = "0.42"
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert not isinstance(app.screen, SetpointScreen)
            assert client.request("status")["set_voltage_v"] == pytest.approx(0.42)
            await pilot.press("i")
            app.screen.query_one(Input).value = "0.671"
            await pilot.press("o")  # typing must not trigger the global output shortcut
            assert owner.worker.device.payload[107] == 1
            app.screen.query_one(Input).value = "0.671"
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert client.request("status")["set_current_a"] == pytest.approx(0.671)
            await pilot.press("o")
            await pilot.pause(0.3)
            assert not client.request("status")["output_enabled"]
            for key, view in (("1", "numbers"), ("2", "charts"), ("3", "combined")):
                await pilot.press(key)
                assert app.view == view
            await pilot.press("[")
            assert app.window_index == 0
            await pilot.press("]")
            assert app.window_index == 1
            for width, height in ((80, 24), (60, 18), (120, 40)):
                await pilot.resize_terminal(width, height)
                await pilot.pause()
                assert app.query_one("Footer").region.bottom <= height
                assert app.query_one("#connection").region.right <= width
            # Closing an attached viewer must not stop its independent service.
            await pilot.press("q")
        assert not owner.worker.stopping.is_set()
        assert client.request("status")["set_current_a"] == pytest.approx(0.671)
    asyncio.run(scenario())


def test_dashboard_sees_changes_from_another_client_and_disables_stale_controls(service):
    owner, client = service
    async def scenario():
        app = Dashboard(client, demo=True)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.2)
            await asyncio.to_thread(client.request, "set", {"voltage": 0.84})
            await pilot.pause(0.3)
            assert app.report["set_voltage_v"] == pytest.approx(0.84)
            app.subscription_failed = True
            before = owner.worker.device.write_attempts
            await pilot.press("v", "o")
            assert not isinstance(app.screen, SetpointScreen)
            assert owner.worker.device.write_attempts == before
    asyncio.run(scenario())


def test_history_gaps_retention_and_zero_values():
    store = StateStore(Identity(None, "demo"))
    store.update({"output_voltage_v": 0, "output_current_a": 0, "output_power_w": 0})
    store.transition("connected")
    history = History()
    start = time.monotonic()
    for i in range(2000):
        report = store.report()
        report["sample_monotonic"] = start + i * 0.5
        history.add(report)
    assert len(history.samples) <= 1201
    report["connection"]["state"] = "disconnected"
    history.add(report)
    report["connection"]["state"] = "connected"
    report["sample_monotonic"] += 0.5
    history.add(report)
    groups = history.series(600, 1, now=report["sample_monotonic"])
    assert len(groups) == 2
    assert all(value == 0 for _, values in groups for value in values)
