"""DPS-150 binary protocol: framing, register types and state decoding."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import struct

VID, PID = 0x2E3C, 0x5740
TX, RX, GET, SET, SESSION = 0xF1, 0xF0, 0xA1, 0xB1, 0xC1
ALL = 0xFF

FLOAT_REGISTERS = {
    0xC0: "input_voltage_v", 0xC1: "set_voltage_v", 0xC2: "set_current_a",
    0xC4: "temperature_c",
    **{0xC5 + i: f"preset_{i // 2 + 1}_{'voltage_v' if i % 2 == 0 else 'current_a'}"
       for i in range(12)},
    0xD1: "ovp_v", 0xD2: "ocp_a", 0xD3: "opp_w", 0xD4: "otp_c",
    0xD5: "lvp_v", 0xD9: "capacity_ah", 0xDA: "energy_wh",
    0xE2: "max_voltage_v", 0xE3: "max_current_a",
}
BYTE_REGISTERS = {
    0xD6: "brightness_raw", 0xD7: "volume_raw", 0xD8: "metering_raw",
    0xDB: "output_raw", 0xDC: "protection_raw", 0xDD: "mode_raw",
    0xE1: "device_address",
}
STRING_REGISTERS = {0xDE: "model", 0xDF: "hardware_version", 0xE0: "firmware_version"}
PROTECTIONS = ("OK", "OVP", "OCP", "OPP", "OTP", "LVP", "REP")
MEASUREMENTS = ("output_voltage_v", "output_current_a", "output_power_w")
CSV_FIELDS = (
    "timestamp_utc", "input_voltage_v", *MEASUREMENTS, "temperature_c",
    "set_voltage_v", "set_current_a", "output_enabled", "mode", "protection",
    "capacity_ah", "energy_wh", "metering_raw", "max_voltage_v", "max_current_a",
    "ovp_v", "ocp_a", "opp_w", "otp_c", "lvp_v",
)


class DPS150Error(Exception):
    """Device, protocol, or validation failure."""


class ProtocolError(DPS150Error):
    """A complete packet has an unsupported or invalid payload."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def encode(command: int, register: int, payload: bytes = b"\x00", *, header: int = TX) -> bytes:
    if header not in (TX, RX) or not 0 <= command <= 255 or not 0 <= register <= 255:
        raise ValueError("invalid packet header, command, or register")
    if len(payload) > 255:
        raise ValueError("payload exceeds the one-byte length field")
    return bytes((header, command, register, len(payload))) + payload + bytes(
        ((register + len(payload) + sum(payload)) & 0xFF,)
    )


@dataclass(frozen=True)
class Frame:
    header: int
    command: int
    register: int
    payload: bytes

    @property
    def raw(self) -> bytes:
        return encode(self.command, self.register, self.payload, header=self.header)


def valid_length(header: int, command: int, register: int, length: int) -> bool:
    if header != RX or command != GET:
        return True
    if register in FLOAT_REGISTERS:
        return length == 4
    if register in BYTE_REGISTERS:
        return length == 1
    if register in STRING_REGISTERS:
        return length > 0
    if register == 0xC3:
        return length == 12
    if register == ALL:
        return length >= 119
    return True


class FrameParser:
    """Incremental framing with checksum validation and recovery after noise.

    An incomplete frame is kept until more bytes arrive. The one-byte length
    bounds retained partial data to 259 bytes. Known register sizes help reject
    damaged length bytes without waiting for a fictitious payload.
    """

    def __init__(self, header: int = RX):
        self.header = header
        self.buffer = bytearray()
        self.discarded_bytes = 0
        self.bad_checksums = 0

    def feed(self, chunk: bytes) -> list[Frame]:
        self.buffer.extend(chunk)
        frames = []
        while self.buffer:
            if self.buffer[0] != self.header:
                del self.buffer[0]
                self.discarded_bytes += 1
                continue
            if len(self.buffer) < 4:
                break
            header, command, register, length = self.buffer[:4]
            if command not in (GET, SET, 0xB0, 0xC0, SESSION) or not valid_length(
                header, command, register, length
            ):
                del self.buffer[0]
                self.discarded_bytes += 1
                continue
            size = length + 5
            if len(self.buffer) < size:
                break
            if sum(self.buffer[2:size - 1]) & 0xFF != self.buffer[size - 1]:
                del self.buffer[0]
                self.bad_checksums += 1
                self.discarded_bytes += 1
                continue
            frames.append(Frame(header, command, register, bytes(self.buffer[4:size - 1])))
            del self.buffer[:size]
        return frames


def float_at(payload: bytes, offset: int = 0) -> float:
    value = struct.unpack_from("<f", payload, offset)[0]
    if not math.isfinite(value):
        raise ProtocolError(f"non-finite float at payload offset {offset}")
    return value


def byte_fields(register: int, value: int) -> dict:
    result = {BYTE_REGISTERS[register]: value}
    if register == 0xDB:
        result["output_enabled"] = {0: False, 1: True}.get(value)
    elif register == 0xDC:
        result["protection"] = PROTECTIONS[value] if value < len(PROTECTIONS) else f"unknown({value})"
    elif register == 0xDD:
        result["mode"] = {0: "CC", 1: "CV"}.get(value, f"unknown({value})")
    return result


def decode_payload(register: int, payload: bytes) -> dict:
    if not valid_length(RX, GET, register, len(payload)):
        raise ProtocolError(f"register 0x{register:02x}: invalid payload size {len(payload)}")
    if register in FLOAT_REGISTERS:
        return {FLOAT_REGISTERS[register]: float_at(payload)}
    if register in BYTE_REGISTERS:
        return byte_fields(register, payload[0])
    if register in STRING_REGISTERS:
        return {STRING_REGISTERS[register]: payload.split(b"\x00", 1)[0].decode("ascii", "replace").strip()}
    if register == 0xC3:
        return {name: float_at(payload, i * 4) for i, name in enumerate(MEASUREMENTS)}
    if register != ALL:
        return {"unknown_payload_hex": payload.hex(" ")}

    # The state block packs bytes and floats without alignment/padding.
    result = {"payload_length": len(payload)}
    first = ("input_voltage_v", "set_voltage_v", "set_current_a", *MEASUREMENTS, "temperature_c")
    result.update({name: float_at(payload, 4 * i) for i, name in enumerate(first)})
    for i in range(12):
        result[FLOAT_REGISTERS[0xC5 + i]] = float_at(payload, 28 + i * 4)
    for i in range(5):
        result[FLOAT_REGISTERS[0xD1 + i]] = float_at(payload, 76 + i * 4)
    for register, offset in ((0xD6, 96), (0xD7, 97), (0xD8, 98), (0xDB, 107), (0xDC, 108), (0xDD, 109)):
        result.update(byte_fields(register, payload[offset]))
    for name, offset in (("capacity_ah", 99), ("energy_wh", 103), ("max_voltage_v", 111), ("max_current_a", 115)):
        result[name] = float_at(payload, offset)
    result["reserved_110_raw"] = payload[110]
    for i, name in enumerate(("ovp_max_v", "ocp_max_a", "opp_max_w", "otp_max_c", "lvp_max_v")):
        offset = 119 + i * 4
        if len(payload) >= offset + 4:
            result[name] = float_at(payload, offset)
    if len(payload) > 139:
        result["extra_payload_hex"] = payload[139:].hex(" ")
    return result
