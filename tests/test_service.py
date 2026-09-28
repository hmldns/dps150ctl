from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from unittest.mock import patch

import pytest

from dps150ctl.cli import DirectConnection, RemoteTrace, main
from dps150ctl.config import Settings
from dps150ctl.demo import DemoDevice
from dps150ctl.driver import DPS150
from dps150ctl.errors import ControlError
from dps150ctl.identity import Identity, select_identity
from dps150ctl.ipc import Client, Endpoint, Ownership, Service, known_services
from dps150ctl.protocol import FrameParser, GET, RX, SESSION, SET, encode
from dps150ctl.state import EventBus, StateStore
from dps150ctl.worker import DeviceWorker
from test_dps150 import SimulatedSerial, STATE


def until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    raise AssertionError("condition did not become true before timeout")


@pytest.fixture
def service():
    identity = Identity(None, "demo")
    endpoint = Endpoint(identity)
    worker = DeviceWorker(identity, factory=DemoDevice, reconnect=False)
    owner = Service(worker, endpoint).start()
    until(lambda: worker.store.report()["connection"]["state"] == "connected")
    try:
        yield owner, Client(endpoint)
    finally:
        owner.close()


def test_concurrent_requests_share_one_owner(service):
    owner, client = service
    with ThreadPoolExecutor(max_workers=8) as pool:
        states = list(pool.map(lambda n: client.request("set", {"voltage": n / 10}), range(1, 9)))
    assert sorted(round(s["set_voltage_v"], 1) for s in states) == [n / 10 for n in range(1, 9)]
    assert owner.worker.device.write_attempts == 8
    assert all(s["output_enabled"] for s in states)
    assert known_services() == [owner.worker.identity]
    with pytest.raises(ControlError, match="another process owns"):
        Ownership(owner.endpoint.lock).acquire()


def test_cli_routes_to_service_without_serial_open(service, monkeypatch):
    owner, client = service
    monkeypatch.setattr("dps150ctl.identity.ports", lambda: [])
    output = io.StringIO()
    with redirect_stdout(output):
        assert main(["--port", "demo", "set", "--voltage", "0.42", "--current", "0.671", "--json"]) == 0
    state = json.loads(output.getvalue())
    assert state["set_voltage_v"] == pytest.approx(0.42)
    assert state["set_current_a"] == pytest.approx(0.671)
    assert client.request("status")["set_voltage_v"] == pytest.approx(0.42)
    assert owner.worker.device.write_attempts == 2


def test_active_owner_error_never_falls_back(service, monkeypatch):
    owner, _ = service
    monkeypatch.setattr("dps150ctl.identity.ports", lambda: [])
    with patch.object(DirectConnection, "__enter__", side_effect=AssertionError("must not fall back")):
        with patch.object(Client, "request", side_effect=ControlError("timeout", "service not responding")):
            output = io.StringIO()
            with redirect_stdout(output):
                assert main(["--port", "demo", "status", "--json"]) == 1
            assert json.loads(output.getvalue())["error"]["code"] == "timeout"


def test_client_lost_response_is_uncertain_and_sent_once():
    endpoint = Endpoint(Identity(None, "unresponsive"))
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(endpoint.socket))
    listener.listen(4)
    requests = []
    def unresponsive():
        connection, _ = listener.accept()
        with connection, connection.makefile("rb") as source:
            requests.append(json.loads(source.readline()))
            time.sleep(0.25)
    thread = threading.Thread(target=unresponsive)
    thread.start()
    try:
        with pytest.raises(ControlError) as failure:
            Client(endpoint, timeout=0.1).request("set", {"voltage": 0.42})
        assert failure.value.code == "uncertain_write"
        assert failure.value.uncertain
    finally:
        thread.join(2)
        listener.close()
    assert len(requests) == 1


def test_expired_queued_write_is_cancelled(service):
    owner, client = service
    started, release = threading.Event(), threading.Event()
    original = owner.worker.controller.execute
    def blocked(operation, params):
        if operation == "status":
            started.set()
            release.wait(2)
        return original(operation, params)
    owner.worker.controller.execute = blocked
    job = owner.worker.submit("status", {})
    assert started.wait(1)
    try:
        with pytest.raises(ControlError):
            Client(owner.endpoint, timeout=0.1).request("set", {"voltage": 0.42})
        time.sleep(0.15)  # handler observes closed peer/expired deadline
    finally:
        release.set()
    job.result(2)
    client.request("status")  # barrier after the cancelled queued operation
    assert owner.worker.device.write_attempts == 0


def test_stale_socket_cleanup_and_startup_race():
    identity = Identity(None, "demo")
    endpoint = Endpoint(identity)
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(endpoint.socket))
    stale.close()
    first = Service(DeviceWorker(identity, factory=DemoDevice), endpoint).start()
    try:
        with pytest.raises(ControlError, match="another process owns"):
            Service(DeviceWorker(identity, factory=DemoDevice), endpoint).start()
        assert Client(endpoint).request("describe")["service"]["pid"] == os.getpid()
    finally:
        first.close()
    assert not endpoint.socket.exists()
    assert not endpoint.metadata.exists()


def test_voltage_cap_everywhere_and_no_partial_changes(service):
    owner, client = service
    device = owner.worker.device
    owner.worker.store.max_voltage = device.max_voltage = 1.0
    attempts = device.write_attempts
    with pytest.raises(ControlError):
        client.request("set", {"voltage": 1.01, "current": 0.5})
    assert device.write_attempts == attempts
    with pytest.raises(ControlError):
        client.request("output", {"enabled": True})  # pre-existing 3.3 V is above the cap
    assert device.write_attempts == attempts
    assert client.request("output", {"enabled": False})["output_enabled"] is False
    assert client.request("set", {"voltage": 0.42})["set_voltage_v"] == pytest.approx(0.42)
    assert client.request("output", {"enabled": True})["output_enabled"] is True
    with pytest.raises(ControlError):
        client.request("set", {"voltage": 1.1, "max_voltage": 30})  # cannot raise owner's cap
    with pytest.raises(ControlError):
        client.request("set", {"voltage": 0.6, "max_voltage": 0.5})
    for bad in (-1, True, "2", float("nan"), float("inf")):
        with pytest.raises((ControlError, ValueError)):
            client.request("set", {"voltage": bad})


def test_direct_caps_and_cleanup_keep_locked():
    simulated = SimulatedSerial()
    with patch("serial.Serial", return_value=simulated):
        with DirectConnection(Identity(None, "/test/serial"), Settings(0.5), timeout=0.05) as connection:
            assert connection.request("status")["limits"]["max_voltage_v"] == 0.5
            with pytest.raises(ControlError):
                connection.request("set", {"voltage": 0.51})
            connection.request("set", {"voltage": 0.42, "current": 0.671})
    assert simulated.closed
    assert encode(SESSION, 0, b"\x00") not in simulated.writes
    assert simulated.payload[107] == 1


def test_uncertain_write_is_not_retried():
    simulated = SimulatedSerial()
    with patch("serial.Serial", return_value=simulated):
        with DirectConnection(Identity(None, "/test/serial"), Settings(), timeout=0.02) as connection:
            original = simulated.write
            def interrupted(packet):
                result = original(packet)
                if packet[1] == SET:
                    simulated.silent_register = 0xFF
                return result
            simulated.write = interrupted
            with pytest.raises(ControlError) as failure:
                connection.request("set", {"voltage": 0.42})
            assert failure.value.uncertain
            assert failure.value.code == "uncertain_write"
    assert len([p for p in simulated.writes if p[1] == SET]) == 1
    assert struct.unpack_from("<f", simulated.payload, 4)[0] == pytest.approx(0.42)


def test_interleaved_telemetry_is_dispatched_including_after_response():
    simulated = SimulatedSerial()
    frames = []
    with patch("serial.Serial", return_value=simulated):
        device = DPS150("/test/serial", timeout=0.02, on_frame=frames.append).open()
        original = simulated.write
        telemetry = encode(GET, 0xC3, struct.pack("<fff", 0.42, 0.2, 0.084), header=RX)
        def combined(packet):
            simulated.pending.extend(telemetry)
            result = original(packet)
            simulated.pending.extend(telemetry)
            return result
        simulated.write = combined
        simulated.read = lambda count: bytes(simulated.pending.pop(0) for _ in range(min(count, len(simulated.pending))))
        device.snapshot()
        assert [f.register for f in frames[-3:]] == [0xC3, 0xFF, 0xC3]
        device.close(keep_locked=True)


def test_unlock_stops_owner_but_preserves_output(service):
    owner, client = service
    device = owner.worker.device
    report = client.request("unlock")
    assert not report["panel_lock_requested"]
    assert not report["panel_lock_verified"]
    assert owner.worker.finished.wait(2)
    owner.close()
    assert device.payload[107] == 1
    assert device.write_attempts == 0


def test_stop_keeps_output_and_lock(service):
    owner, client = service
    device = owner.worker.device
    assert client.request("stop")["panel_lock_requested"]
    assert owner.worker.finished.wait(2)
    assert device.payload[107] == 1
    assert device.write_attempts == 0
    assert owner.worker.store.panel_lock_requested


def test_reconnect_tracks_serial_not_tty_and_never_replays(monkeypatch):
    connected = [{"port": "/dev/ttyACM0", "serial_number": "TEST-SERIAL", "vid": 0x2e3c,
                  "pid": 0x5740, "candidate": True}]
    monkeypatch.setattr("dps150ctl.identity.ports", lambda: list(connected))
    devices = []
    def factory(*args, **kwargs):
        device = DemoDevice(*args, **kwargs)
        devices.append(device)
        return device
    worker = DeviceWorker(Identity("TEST-SERIAL", "/dev/ttyACM0"), factory=factory)
    worker.start()
    try:
        until(lambda: worker.store.connection == "connected")
        worker.submit("set", {"voltage": 0.42}).result(2)
        connected[0] = {**connected[0], "serial_number": "DIFFERENT", "port": "/dev/ttyACM0"}
        devices[0]._receive = lambda: (_ for _ in ()).throw(OSError("USB disconnected"))
        until(lambda: worker.store.connection == "reconnecting")
        with pytest.raises(ControlError):
            worker.submit("output", {"enabled": True}).result(2)
        time.sleep(1.1)
        assert len(devices) == 1  # the replacement bridge was never opened
        connected[:] = [{**connected[0], "serial_number": "TEST-SERIAL", "port": "/dev/ttyACM7"}]
        until(lambda: len(devices) == 2 and worker.store.connection == "connected", timeout=4)
        assert devices[1].port == "/dev/ttyACM7"
        assert devices[1].write_attempts == 0
        assert worker.store.report()["identity"]["serial_number"] == "TEST-SERIAL"
        assert worker.store.generation == 2
    finally:
        worker.stop()
        worker.join()


def test_no_serial_identity_disables_reconnect():
    devices = []
    def factory(*args, **kwargs):
        device = DemoDevice(*args, **kwargs)
        devices.append(device)
        return device
    worker = DeviceWorker(Identity(None, "demo"), factory=factory)
    worker.start()
    try:
        until(lambda: worker.store.connection == "connected")
        devices[0]._receive = lambda: (_ for _ in ()).throw(OSError("disconnected"))
        until(lambda: worker.store.connection == "error")
        time.sleep(1.1)
        assert len(devices) == 1
    finally:
        worker.stop()
        worker.join()


def test_disconnected_known_service_remains_selectable(monkeypatch):
    monkeypatch.setattr("dps150ctl.identity.ports", lambda: [])
    known = Identity("OFFLINE-SERIAL", "/dev/ttyACM5")
    assert select_identity(known=[known]) == known
    with pytest.raises(ControlError, match="multiple"):
        select_identity(known=[known, Identity("OTHER")])


def test_config_precedence_and_invalid_values(isolated_environment):
    config = isolated_environment / "config.toml"
    config.write_text("max_voltage = 0.84\n")
    assert Settings.load(config, max_voltage=0.42).max_voltage == 0.42
    assert Settings.load(config, max_voltage=30).max_voltage == 0.84
    config.write_text("max_voltage = nan\n")
    with pytest.raises(ControlError):
        Settings.load(config)
    config.write_text("max_volatge = 1\n")
    with pytest.raises(ControlError):
        Settings.load(config)


def test_slow_capture_fails_explicitly_without_blocking_owner():
    bus = EventBus()
    subscriber = bus.subscribe({"trace"})
    for i in range(300):
        bus.publish("trace", {"number": i})
    assert subscriber.closed
    assert subscriber.events.get_nowait()["data"]["code"] == "slow_consumer"


def test_state_freshness_does_not_refresh_on_temperature():
    store = StateStore(Identity(None, "demo"))
    store.update({"output_voltage_v": 0.42, "output_current_a": 0, "output_power_w": 0})
    store.transition("connected")
    store.sample_time = time.monotonic() - 3
    store.update({"temperature_c": 30})
    assert store.report()["connection"]["state"] == "stale"


def test_remote_capture_is_decodable(service):
    owner, client = service
    file = io.StringIO()
    with RemoteTrace(client, file) as recording:
        owner.worker.trace("rx", STATE)
        until(lambda: recording.count == 1)
    lines = [json.loads(line) for line in file.getvalue().splitlines()]
    assert bytes.fromhex(lines[0]["hex"]) == STATE


class PseudoSupply:
    """Real pyserial/PTY integration, with a captured full-state reply."""
    def __init__(self):
        self.master, self.slave = os.openpty()
        self.path = os.ttyname(self.slave)
        self.done = threading.Event()
        self.packets = []
        self.payload = bytearray(STATE[4:-1])
        self.payload[107] = 1
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        import select
        parser = FrameParser(0xF1)
        while not self.done.is_set():
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if not ready:
                continue
            try:
                data = os.read(self.master, 4096)
                for frame in parser.feed(data):
                    self.packets.append(frame)
                    if frame.command == GET:
                        payload = {0xDE: b"DPS-150", 0xDF: b"V1.0", 0xE0: b"V1.2", 0xFF: bytes(self.payload)}.get(frame.register)
                        if payload is not None:
                            os.write(self.master, encode(GET, frame.register, payload, header=RX))
                    elif frame.command == SET:
                        if frame.register in (0xC1, 0xC2):
                            offset = 4 if frame.register == 0xC1 else 8
                            self.payload[offset:offset+4] = frame.payload
                        elif frame.register == 0xDB:
                            self.payload[107] = frame.payload[0]
            except OSError:
                return

    def close(self):
        self.done.set()
        self.thread.join(2)
        os.close(self.master)
        os.close(self.slave)


def test_real_process_daemon_cli_and_sigterm():
    supply = PseudoSupply()
    command = [sys.executable, "-m", "dps150ctl", "--port", supply.path]
    process = subprocess.Popen(command + ["daemon", "run"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        endpoint = Endpoint(Identity(None, supply.path))
        until(endpoint.socket.exists)
        client = Client(endpoint)
        until(lambda: client.request("describe")["connection"]["state"] == "connected")
        result = subprocess.run(command + ["set", "--voltage", "0.42", "--current", "0.671", "--json"],
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr + result.stdout
        assert json.loads(result.stdout)["set_voltage_v"] == pytest.approx(0.42)
        assert len([f for f in supply.packets if f.command == SESSION]) == 1
        process.terminate()
        assert process.wait(timeout=5) == 0
        assert supply.payload[107] == 1
        assert not any(f.command == SESSION and f.payload == b"\x00" for f in supply.packets)
        assert not endpoint.socket.exists()
        # No daemon: a subsequent process acquires the same lock and opens directly.
        direct = subprocess.run(command + ["status", "--json"], capture_output=True, text=True, timeout=10)
        assert direct.returncode == 0, direct.stderr + direct.stdout
        assert json.loads(direct.stdout)["set_current_a"] == pytest.approx(0.671)
        assert len([f for f in supply.packets if f.command == SESSION]) == 2
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        supply.close()
