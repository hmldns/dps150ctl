"""Raw JSONL capture format and offline packet decoding."""
import json
from pathlib import Path
import sys
import time
from .protocol import DPS150Error, FrameParser, GET, ProtocolError, RX, TX, decode_payload, utc_now

class TraceWriter:
    """JSONL raw serial chunks; these can be replayed by the decode command."""

    def __init__(self, file):
        self.file = file
        self.start = time.monotonic()

    def __call__(self, direction: str, data: bytes) -> None:
        self.file.write(json.dumps({
            "timestamp_utc": utc_now(), "elapsed_s": time.monotonic() - self.start,
            "direction": direction, "hex": data.hex(" "),
        }) + "\n")
        self.file.flush()

def decode_trace(path: Path) -> None:
    parsers = {"rx": FrameParser(RX), "tx": FrameParser(TX)}
    with path.open() as source:
        for number, line in enumerate(source, 1):
            try:
                record = json.loads(line)
                direction = record["direction"]
                parser = parsers[direction]
                chunk = bytes.fromhex(record["hex"])
            except (ValueError, KeyError, TypeError) as exc:
                raise DPS150Error(f"invalid trace record at line {number}: {exc}") from exc
            for frame in parser.feed(chunk):
                item = {"timestamp_utc": record.get("timestamp_utc"), "direction": direction,
                        "command": f"0x{frame.command:02x}", "register": f"0x{frame.register:02x}",
                        "payload_hex": frame.payload.hex(" "), "packet_hex": frame.raw.hex(" ")}
                if direction == "rx" and frame.command == GET:
                    try:
                        item["decoded"] = decode_payload(frame.register, frame.payload)
                    except ProtocolError as exc:
                        item["decode_error"] = str(exc)
                print(json.dumps(item, allow_nan=False), flush=True)
    for direction, parser in parsers.items():
        print(f"{direction}: discarded={parser.discarded_bytes}, bad_checksums={parser.bad_checksums}, "
              f"trailing_bytes={len(parser.buffer)}", file=sys.stderr)
