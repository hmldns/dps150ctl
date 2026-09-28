# /// script
# requires-python = ">=3.11"
# dependencies = ["pyserial==3.5"]
# ///
"""Offline regression tests, including packets captured from the actual V1.2 unit."""

from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import random
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dps150 as dps

FIXTURES = Path(__file__).parent / "fixtures"
MODEL = bytes.fromhex("f0 a1 de 07 44 50 53 2d 31 35 30 8f")
HARDWARE = bytes.fromhex("f0 a1 df 04 56 31 2e 30 c8")
FIRMWARE = bytes.fromhex("f0 a1 e0 04 56 31 2e 32 cb")


def trace_bytes(name, direction="rx"):
    records = [json.loads(line) for line in (FIXTURES / name).read_text().splitlines()]
    return b"".join(bytes.fromhex(r["hex"]) for r in records if r["direction"] == direction)


STATE_STREAM = trace_bytes("v1.2-state.jsonl")
STATE_START = STATE_STREAM.index(bytes.fromhex("f0 a1 ff 8b"))
STATE = STATE_STREAM[STATE_START:STATE_START + 144]


class CodecTests(unittest.TestCase):
    def test_wire_vectors(self):
        for command, register, payload, expected in (
            (dps.SESSION, 0, b"\x01", "f1c100010102"),
            (dps.SESSION, 0, b"\x00", "f1c100010001"),
            (dps.GET, 0xFF, b"\x00", "f1a1ff010000"),
            (dps.GET, 0xDE, b"\x00", "f1a1de0100df"),
            (dps.SET, 0xC1, struct.pack("<f", 5), "f1b1c1040000a040a5"),
            (dps.SET, 0xC2, struct.pack("<f", 1), "f1b1c2040000803f85"),
            (dps.SET, 0xDB, b"\x00", "f1b1db0100dc"),
            (dps.SET, 0xDB, b"\x01", "f1b1db0101dd"),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(dps.encode(command, register, payload).hex(), expected)

    def test_all_possible_splits_of_real_state(self):
        for split in range(len(STATE) + 1):
            parser = dps.FrameParser()
            frames = parser.feed(STATE[:split]) + parser.feed(STATE[split:])
            self.assertEqual([f.raw for f in frames], [STATE])
            self.assertEqual(parser.buffer, b"")

    def test_real_frames_with_random_chunking(self):
        data = trace_bytes("v1.2-info.jsonl") + STATE_STREAM
        for seed in range(20):
            rng, parser, frames, offset = random.Random(seed), dps.FrameParser(), [], 0
            while offset < len(data):
                size = rng.randint(1, 64)
                frames.extend(parser.feed(data[offset:offset + size]))
                offset += size
            self.assertEqual(len(frames), 9)
            self.assertEqual(parser.bad_checksums, 0)
            self.assertEqual(parser.discarded_bytes, 0)
            self.assertEqual(parser.buffer, b"")

    def test_recovery_after_garbage_checksum_and_bad_length(self):
        corrupt = bytearray(MODEL)
        corrupt[-1] ^= 1
        parser = dps.FrameParser()
        data = b"garbage" + bytes(corrupt) + bytes.fromhex("f0 a1 c0 ff") + MODEL + STATE
        self.assertEqual([f.raw for f in parser.feed(data)], [MODEL, STATE])
        self.assertEqual(parser.bad_checksums, 1)
        self.assertGreater(parser.discarded_bytes, 0)

    def test_unknown_register_is_preserved(self):
        packet = dps.encode(dps.GET, 0x42, b"\x10\x20", header=dps.RX)
        frame = dps.FrameParser().feed(packet)[0]
        self.assertEqual(dps.decode_payload(frame.register, frame.payload), {"unknown_payload_hex": "10 20"})

    def test_captured_state_layout_and_unaligned_values(self):
        state = dps.decode_payload(0xFF, STATE[4:-1])
        self.assertEqual(state["payload_length"], 139)
        self.assertAlmostEqual(state["input_voltage_v"], 4.9898529052734375)
        self.assertAlmostEqual(state["set_voltage_v"], 3.399402379989624)
        self.assertEqual(state["preset_6_voltage_v"], 12)
        self.assertEqual(state["preset_2_voltage_v"], 1)
        self.assertEqual(state["set_current_a"], 1)
        self.assertEqual(state["capacity_ah"], 0)
        self.assertEqual(state["energy_wh"], 0)
        self.assertFalse(state["output_enabled"])
        self.assertEqual(state["protection"], "LVP")
        self.assertEqual(state["mode"], "CV")
        self.assertEqual(state["metering_raw"], 1)
        self.assertNotIn("metering_enabled", state)
        self.assertEqual(state["otp_max_c"], 99)
        self.assertAlmostEqual(state["max_current_a"], 5.1, places=5)
        self.assertAlmostEqual(state["max_voltage_v"], 4.789853096008301)

    def test_optional_trailer_and_short_state(self):
        old = dps.decode_payload(0xFF, STATE[4:4 + 119])
        self.assertNotIn("ovp_max_v", old)
        extended = dps.decode_payload(0xFF, STATE[4:-1] + b"\x99\xaa")
        self.assertEqual(extended["extra_payload_hex"], "99 aa")
        with self.assertRaises(dps.ProtocolError):
            dps.decode_payload(0xFF, b"\x00" * 118)

    def test_nonfinite_or_malformed_readings_are_not_silently_accepted(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(dps.ProtocolError):
                dps.decode_payload(0xC0, struct.pack("<f", value))
        with self.assertRaises(dps.ProtocolError):
            dps.decode_payload(0xC3, b"\x00" * 8)
        self.assertEqual(dps.decode_payload(0xDC, b"\xff")["protection"], "unknown(255)")
        self.assertIsNone(dps.decode_payload(0xDB, b"\x03")["output_enabled"])


class SimulatedSerial:
    """Small deterministic transport using captured replies, with output initially ON."""

    def __init__(self):
        self.pending = bytearray()
        self.writes = []
        self.closed = False
        self.payload = bytearray(STATE[4:-1])
        self.payload[107] = 1
        self.payload[108] = 0
        self.ignore_writes = False
        self.silent_register = None
        self.model_reply = MODEL

    @property
    def in_waiting(self):
        return len(self.pending)

    def read(self, count):
        count = min(count, 7)  # exercise fragmented serial reads
        result = bytes(self.pending[:count])
        del self.pending[:count]
        return result

    def write(self, packet):
        self.writes.append(packet)
        _, command, register, length = packet[:4]
        self.assert_packet(packet, length)
        if command == dps.GET and register != self.silent_register:
            replies = {0xDE: self.model_reply, 0xDF: HARDWARE, 0xE0: FIRMWARE,
                       0xFF: dps.encode(dps.GET, 0xFF, bytes(self.payload), header=dps.RX)}
            self.pending.extend(replies.get(register, b""))
        elif command == dps.SET and not self.ignore_writes:
            if register in (0xC1, 0xC2):
                offset = {0xC1: 4, 0xC2: 8}[register]
                self.payload[offset:offset + 4] = packet[4:8]
            elif register == 0xDB:
                self.payload[107] = packet[4]
        return len(packet)

    @staticmethod
    def assert_packet(packet, length):
        assert len(packet) == length + 5
        assert packet[-1] == sum(packet[2:-1]) % 256

    def close(self):
        self.closed = True


class DriverTests(unittest.TestCase):
    def setUp(self):
        runtime = tempfile.TemporaryDirectory(prefix="dps150-test-")
        self.addCleanup(runtime.cleanup)
        environment = patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime.name, "XDG_CONFIG_HOME": runtime.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.transport = SimulatedSerial()
        patcher = patch.object(dps.serial, "Serial", return_value=self.transport)
        self.factory = patcher.start()
        self.addCleanup(patcher.stop)
        sleep = patch.object(dps.time, "sleep")
        sleep.start()
        self.addCleanup(sleep.stop)

    def device(self):
        return dps.DPS150("/test/serial", timeout=0.01)

    def test_read_session_does_not_modify_live_output_or_settings(self):
        before = bytes(self.transport.payload)
        with self.device() as device:
            self.assertEqual(device.info()["firmware_version"], "V1.2")
            self.assertTrue(device.snapshot()["output_enabled"])
        self.assertEqual(bytes(self.transport.payload), before)
        self.assertTrue(self.transport.closed)
        self.assertFalse(any(packet[1] == dps.SET for packet in self.transport.writes))
        self.assertEqual(self.transport.writes[-1].hex(), "f1c100010001")
        self.assertFalse(self.factory.call_args.kwargs["rtscts"])

    def test_validate_all_changes_before_any_write(self):
        with self.device() as device:
            for kwargs in ({"voltage": 5, "current": 0.1}, {"voltage": 1, "current": 5.01},
                           {"voltage": float("nan")}, {"current": -1}, {"current": float("inf")}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    device.configure(**kwargs)
        self.assertFalse(any(p[1] == dps.SET for p in self.transport.writes))

    def test_configure_current_then_voltage_with_readback(self):
        with self.device() as device:
            result = device.configure(voltage=3.3, current=0.25)
        writes = [p for p in self.transport.writes if p[1] == dps.SET]
        self.assertEqual([p[2] for p in writes], [0xC2, 0xC1])
        self.assertAlmostEqual(result["set_voltage_v"], 3.3, places=5)
        self.assertEqual(result["set_current_a"], 0.25)
        self.assertTrue(result["output_enabled"])

    def test_write_rejection_is_reported(self):
        self.transport.ignore_writes = True
        with self.device() as device:
            with self.assertRaisesRegex(dps.DPS150Error, "verification failed"):
                device.set_voltage(2)
            with self.assertRaisesRegex(dps.DPS150Error, "output readback"):
                device.set_output(False)

    def test_explicit_output_command(self):
        with self.device() as device:
            self.assertFalse(device.set_output(False)["output_enabled"])
            self.assertTrue(device.set_output(True)["output_enabled"])
            with self.assertRaises(ValueError):
                device.set_output(1)

    def test_wrong_model_closes_port(self):
        self.transport.model_reply = dps.encode(dps.GET, 0xDE, b"IPS3608", header=dps.RX)
        with self.assertRaisesRegex(dps.DPS150Error, "unexpected model"):
            with self.device():
                self.fail("must not connect to another model")
        self.assertTrue(self.transport.closed)
        self.assertFalse(any(p[1] == dps.SET for p in self.transport.writes))

    def test_missing_snapshot_fails_instead_of_returning_cached_state(self):
        with self.device() as device:
            device.snapshot()
            self.transport.silent_register = 0xFF
            with self.assertRaisesRegex(dps.DPS150Error, "timeout waiting for register 0xff"):
                device.snapshot()

    def test_cli_csv_and_json_are_parseable(self):
        for fmt in ("csv", "jsonl"):
            stream = io.StringIO()
            with redirect_stdout(stream):
                rc = dps.main(["--port", "/test/serial", "stream", "--count", "2", "--format", fmt])
            self.assertEqual(rc, 0)
            if fmt == "csv":
                rows = list(dps.csv.DictReader(io.StringIO(stream.getvalue())))
            else:
                rows = [json.loads(line) for line in stream.getvalue().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertIn("output_voltage_v", rows[0])

    def test_cli_decode_never_opens_a_serial_port(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = dps.main(["decode", str(FIXTURES / "v1.2-info.jsonl")])
        self.assertEqual(result, 0)
        self.factory.assert_not_called()
        records = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(len(records), 8)
        self.assertEqual([r["decoded"] for r in records if "decoded" in r],
                         [{"model": "DPS-150"}, {"hardware_version": "V1.0"}, {"firmware_version": "V1.2"}])

    def test_panel_lock_and_unlock_preserve_output_and_settings(self):
        for command in ("lock", "unlock"):
            self.transport.writes.clear()
            before = bytes(self.transport.payload)
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                result = dps.main(["--port", "/test/serial", command, "--json"])
            self.assertEqual(result, 0)
            self.assertEqual(bytes(self.transport.payload), before)
            self.assertTrue(self.transport.closed)
            self.assertFalse(any(p[1] == dps.SET for p in self.transport.writes))
            sessions = [p.hex() for p in self.transport.writes if p[1] == dps.SESSION]
            expected = ["f1c100010102"]
            if command == "unlock":
                expected.append("f1c100010001")
            self.assertEqual(sessions, expected)
            report = json.loads(stdout.getvalue())
            self.assertEqual(report["panel_lock_requested"], command == "lock")
            self.assertFalse(report["panel_lock_verified"])

    def test_keep_locked_read_skips_session_release(self):
        with redirect_stdout(io.StringIO()):
            result = dps.main(["--port", "/test/serial", "--keep-locked", "status", "--json"])
        self.assertEqual(result, 0)
        self.assertTrue(self.transport.closed)
        self.assertNotIn(bytes.fromhex("f1c100010001"), self.transport.writes)

    def test_keep_locked_error_still_cleans_up_connection(self):
        self.transport.silent_register = 0xFF
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = dps.main(["--port", "/test/serial", "--timeout", "0.01",
                              "--keep-locked", "status", "--json"])
        self.assertEqual(result, 1)
        self.assertTrue(self.transport.closed)
        self.assertNotIn(bytes.fromhex("f1c100010001"), self.transport.writes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
