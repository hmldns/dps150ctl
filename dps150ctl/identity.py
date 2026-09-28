"""USB identity survives tty renumbering; no guessing between identical bridges."""
from dataclasses import asdict, dataclass, replace
import hashlib
import os
from pathlib import Path

from .driver import ports
from .errors import ControlError
from .protocol import PID, VID


@dataclass(frozen=True)
class Identity:
    serial_number: str | None
    port: str | None = None
    vid: int = VID
    pid: int = PID
    location: str | None = None
    interface: str | None = None

    @property
    def key(self) -> str:
        token = (f"{self.vid:04x}:{self.pid:04x}:{self.serial_number}" if self.serial_number
                 else f"port:{os.path.realpath(self.port or '')}")
        return hashlib.sha256(token.encode()).hexdigest()[:24]

    def as_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_port(cls, item: dict) -> "Identity":
        return cls(item.get("serial_number"), os.path.realpath(item["port"]),
                   item.get("vid") or VID, item.get("pid") or PID,
                   item.get("location"), item.get("interface"))

    def resolve(self) -> "Identity":
        if not self.serial_number:
            return self
        matches = [p for p in ports() if (p["vid"], p["pid"], p["serial_number"]) ==
                   (self.vid, self.pid, self.serial_number)]
        if not matches:
            raise ControlError("not_found", f"USB device {self.serial_number} is disconnected")
        if len(matches) > 1:
            raise ControlError("ambiguous", f"multiple tty devices claim USB serial {self.serial_number}")
        return replace(self, port=matches[0]["port"], location=matches[0].get("location"),
                       interface=matches[0].get("interface"))


def select_identity(*, serial_number: str | None = None, port: str | None = None,
                    known: list[Identity] = ()) -> Identity:
    port = port or os.environ.get("DPS150_PORT")
    if port:
        path = os.path.realpath(port)
        matched = [Identity.from_port(p) for p in ports() if os.path.realpath(p["port"]) == path]
        matched += [i for i in known if i.port and os.path.realpath(i.port) == path]
        chosen = matched[0] if matched else Identity(None, path)
        if serial_number and chosen.serial_number != serial_number:
            raise ControlError("identity_mismatch", "--port does not match the requested USB serial")
        return chosen
    candidates = {i.key: i for i in known}
    candidates.update({i.key: i for p in ports() if p["candidate"] for i in [Identity.from_port(p)]})
    if serial_number:
        matches = [i for i in candidates.values() if i.serial_number == serial_number]
        if not matches:
            # Explicit stable identity permits a service to wait for a device.
            return Identity(serial_number)
        candidates = {i.key: i for i in matches}
    if not candidates:
        raise ControlError("not_found", "no DPS-150 found; use 'ports', --device USB_SERIAL, or --port")
    if len(candidates) != 1:
        raise ControlError("ambiguous", "multiple devices/services found; select one with --device or --port")
    return next(iter(candidates.values()))


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    path = Path(base) / "dps150ctl" if base else Path(f"/tmp/dps150ctl-{os.getuid()}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    stat = path.lstat()
    if path.is_symlink() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise ControlError("permission", f"runtime directory must be owned by you with mode 0700: {path}")
    return path
