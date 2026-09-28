"""Synthetic DPS-150 backend. No serial ports or production sockets are used."""
import math
import struct
import time

from .driver import DPS150
from .protocol import ALL, Frame, GET, RX, SET, decode_payload, utc_now


class DemoDevice(DPS150):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.model = "DPS-150 (demo)"
        self.started = time.monotonic()
        self.payload = bytearray(139)
        for offset, value in ((0, 20), (4, 3.3), (8, 0.671), (24, 29),
                              (76, 30), (80, 5), (84, 150), (88, 80), (92, 5),
                              (111, 19.8), (115, 5.1)):
            struct.pack_into("<f", self.payload, offset, value)
        self.payload[107], self.payload[109] = 1, 1
        self.last_sample = 0.0

    def open(self):
        self.serial = True
        return self

    def close(self, *, keep_locked=False):
        self.serial = None

    def _send(self, command, register, payload=b"\x00"):
        if command != SET:
            return
        self.write_attempts += 1
        if register in (0xC1, 0xC2):
            offset = 4 if register == 0xC1 else 8
            self.payload[offset:offset + 4] = payload
        elif register == 0xDB:
            self.payload[107] = payload[0]

    def _sample(self):
        elapsed = time.monotonic() - self.started
        voltage = struct.unpack_from("<f", self.payload, 4)[0] if self.payload[107] else 0
        limit = struct.unpack_from("<f", self.payload, 8)[0]
        current = min(limit, 0.22 + 0.09 * math.sin(elapsed * 0.7)) if self.payload[107] else 0
        measured = max(0, voltage + 0.008 * math.sin(elapsed * 1.8)) if voltage else 0
        struct.pack_into("<fff", self.payload, 12, measured, current, measured * current)

    def read_register(self, register):
        self._sample()
        if register == ALL:
            payload = bytes(self.payload)
        elif register in (0xDE, 0xDF, 0xE0):
            payload = {0xDE: b"DPS-150", 0xDF: b"DEMO", 0xE0: b"DEMO"}[register]
        elif register == 0xC3:
            payload = bytes(self.payload[12:24])
        elif 0xC0 <= register <= 0xC2:
            offset = (register - 0xC0) * 4
            payload = bytes(self.payload[offset:offset + 4])
        else:
            raise ValueError("demo supports state, measurements, setpoints and identity reads")
        frame = Frame(RX, GET, register, payload)
        if self.on_frame:
            self.on_frame(frame)
        return frame

    def snapshot(self):
        frame = self.read_register(ALL)
        return {"timestamp_utc": utc_now(), **decode_payload(ALL, frame.payload)}

    def info(self):
        return {"model": self.model, "hardware_version": "DEMO", "firmware_version": "DEMO"}

    def _receive(self):
        time.sleep(0.04)
        if time.monotonic() - self.last_sample >= 0.5:
            self.last_sample = time.monotonic()
            return [self.read_register(0xC3)]
        return []
