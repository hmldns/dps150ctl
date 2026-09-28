"""Synchronous, single-owner DPS-150 transport. No automatic write retries."""
from __future__ import annotations
import math
import os
import struct
import time
from typing import Callable, Iterator
import serial
from serial.tools import list_ports
from .protocol import (ALL, DPS150Error, Frame, FrameParser, GET, PID, ProtocolError, SESSION, SET,
    VID, decode_payload, encode, utc_now)

def ports() -> list[dict]:
    return [
        {"port": p.device, "description": p.description, "vid": p.vid,
         "pid": p.pid, "serial_number": p.serial_number, "location": p.location,
         "interface": p.interface,
         "candidate": (p.vid, p.pid) == (VID, PID)}
        for p in sorted(list_ports.comports(), key=lambda p: p.device)
    ]


def select_port(port: str | None = None) -> str:
    explicit = port or os.environ.get("DPS150_PORT")
    if explicit:
        return explicit
    candidates = [p["port"] for p in ports() if p["candidate"]]
    if not candidates:
        raise DPS150Error("no 2e3c:5740 serial port found; use 'ports' or --port /dev/ttyACM0")
    if len(candidates) > 1:
        raise DPS150Error(f"multiple candidate ports: {', '.join(candidates)}; select one with --port")
    return candidates[0]

class DPS150:
    """Synchronous single-owner driver; use as a context manager.

    No threads, automatic retries of writes, or implicit changes to output.
    A timeout after a write means its outcome is uncertain; inspect the device.
    """

    def __init__(self, port: str | None = None, *, timeout: float = 2.0,
                 rtscts: bool = False, trace: Callable[[str, bytes], None] | None = None,
                 on_frame: Callable[[Frame], None] | None = None,
                 max_voltage: float | None = None, keep_locked_on_error: bool = False):
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        if max_voltage is not None and (not math.isfinite(max_voltage) or max_voltage < 0):
            raise ValueError("max_voltage must be finite and nonnegative")
        self.port = port
        self.timeout = timeout
        self.rtscts = rtscts
        self.trace = trace
        self.on_frame = on_frame
        self.max_voltage = max_voltage
        self.keep_locked_on_error = keep_locked_on_error
        self.write_attempts = 0
        self.serial = None
        self.parser = FrameParser()
        self.last_write = 0.0
        self.model = None

    def __enter__(self) -> DPS150:
        return self.open()

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.close()
        except (DPS150Error, serial.SerialException, OSError):
            if exc_type is None:
                raise

    def open(self) -> DPS150:
        if self.serial is not None:
            raise DPS150Error("connection is already open")
        self.port = select_port(self.port)
        self.parser = FrameParser()
        self.serial = serial.Serial(
            port=self.port, baudrate=115200, bytesize=8, parity="N", stopbits=1,
            timeout=0.05, write_timeout=self.timeout, rtscts=self.rtscts,
            xonxoff=False, dsrdtr=False, exclusive=True if os.name == "posix" else None,
        )
        try:
            # pyserial asserts DTR and RTS on opening; RTS/CTS is optional.
            self._send(SESSION, 0, b"\x01")
            self.model = decode_payload(0xDE, self.read_register(0xDE).payload)["model"]
            if self.model.upper().replace("-", "").replace(" ", "") != "DPS150":
                raise DPS150Error(f"unexpected model {self.model!r}; expected DPS-150")
        except BaseException:
            try:
                self.close(keep_locked=self.keep_locked_on_error)
            except Exception:
                pass
            raise
        return self

    def close(self, *, keep_locked: bool = False) -> None:
        """Close the serial port, optionally leaving the remote session engaged.

        The manual describes remote mode as a front-panel lock. No readable
        lock-status register is known; verify the icon/buttons on the device.
        A subsequent normal connection/close releases the remote session.
        """
        if self.serial is not None:
            try:
                if not keep_locked:
                    # Session close releases remote control, not the output relay.
                    self._send(SESSION, 0, b"\x00")
                time.sleep(0.05)
            finally:
                self.serial.close()
                self.serial = None

    def _send(self, command: int, register: int, payload: bytes = b"\x00") -> None:
        if self.serial is None:
            raise DPS150Error("connection is not open")
        delay = 0.05 - (time.monotonic() - self.last_write)
        if delay > 0:
            time.sleep(delay)
        packet = encode(command, register, payload)
        if command == SET:
            self.write_attempts += 1
        written = self.serial.write(packet)
        self.last_write = time.monotonic()
        if self.trace:
            self.trace("tx", packet[:written])
        if written != len(packet):
            raise DPS150Error("short serial write; command outcome is uncertain")

    def _receive(self) -> list[Frame]:
        if self.serial is None:
            raise DPS150Error("connection is not open")
        data = self.serial.read(max(1, min(self.serial.in_waiting, 4096)))
        if data and self.trace:
            self.trace("rx", data)
        frames = self.parser.feed(data)
        # Dispatch the entire batch, including frames following a matching reply.
        # A request must not swallow interleaved or already queued telemetry.
        if self.on_frame:
            for frame in frames:
                self.on_frame(frame)
        return frames

    def read_register(self, register: int) -> Frame:
        # Consume already queued telemetry before sending a new request.
        deadline = time.monotonic() + 0.1
        while self.serial is not None and self.serial.in_waiting and time.monotonic() < deadline:
            self._receive()
        self._send(GET, register)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            for frame in self._receive():
                if frame.command == GET and frame.register == register:
                    return frame
        raise DPS150Error(
            f"timeout waiting for register 0x{register:02x} on {self.port} "
            f"(bad checksums: {self.parser.bad_checksums}); close other serial apps "
            "and check power/data cable"
        )

    def info(self) -> dict:
        result = {"port": self.port, "model": self.model}
        for register in (0xDF, 0xE0):
            result.update(decode_payload(register, self.read_register(register).payload))
        return result

    def snapshot(self) -> dict:
        frame = self.read_register(ALL)
        return {"timestamp_utc": utc_now(), **decode_payload(ALL, frame.payload)}

    def frames(self, duration: float) -> Iterator[Frame]:
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError("duration must be positive and finite")
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            yield from self._receive()

    def configure(self, *, voltage: float | None = None, current: float | None = None) -> dict:
        if voltage is None and current is None:
            raise ValueError("provide voltage and/or current")
        state = self.snapshot()
        # Validate every argument before making any changes. Current goes first.
        changes = []
        for value, register, field, limit, physical_max, tolerance in (
            (current, 0xC2, "set_current_a", "max_current_a", 5.0, 0.0011),
            (voltage, 0xC1, "set_voltage_v", "max_voltage_v", 30.0, 0.011),
        ):
            if value is None:
                continue
            maximum = min(state[limit], physical_max)
            if register == 0xC1 and self.max_voltage is not None:
                maximum = min(maximum, self.max_voltage)
            if not math.isfinite(state[limit]) or state[limit] < 0:
                raise ProtocolError(f"invalid device limit: {limit}")
            if not math.isfinite(value) or not 0 <= value <= maximum:
                raise ValueError(f"{field} must be finite and in 0..{maximum:g}")
            changes.append((value, register, field, tolerance))
        for value, register, _, _ in changes:
            self._send(SET, register, struct.pack("<f", value))
        observed = self.snapshot()
        for value, _, field, tolerance in changes:
            if not math.isclose(observed[field], value, rel_tol=0, abs_tol=tolerance):
                raise DPS150Error(f"write verification failed for {field}: requested {value}, read {observed[field]}; settings may have changed")
        return observed

    def set_voltage(self, volts: float) -> dict:
        return self.configure(voltage=volts)

    def set_current(self, amps: float) -> dict:
        return self.configure(current=amps)

    def set_output(self, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise ValueError("enabled must be a bool")
        if enabled:
            state = self.snapshot()
            maximum = min(30.0, state["max_voltage_v"])
            if self.max_voltage is not None:
                maximum = min(maximum, self.max_voltage)
            if not 0 <= state["set_voltage_v"] <= maximum:
                raise ValueError(f"existing voltage setpoint exceeds effective limit {maximum:g} V")
        self._send(SET, 0xDB, bytes((enabled,)))
        state = self.snapshot()
        if state["output_enabled"] is not enabled:
            raise DPS150Error("output readback did not match the request; inspect protection status")
        return state
