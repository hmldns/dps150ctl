"""Shared validated operations for local CLI and the serial-owner worker."""
import math

from .config import tighter
from .errors import ControlError, control_error
from .protocol import decode_payload

OPERATIONS = {
    "status": set(), "info": set(), "read": {"register"},
    "set": {"voltage", "current", "max_voltage"},
    "output": {"enabled", "max_voltage"}, "lock": set(), "unlock": set(),
}
WRITES = {"set", "output"}


def validate_request(operation: str, params: dict):
    if operation not in OPERATIONS:
        raise ControlError("invalid_argument", f"unknown operation: {operation}")
    if not isinstance(params, dict) or params.keys() - OPERATIONS[operation]:
        raise ControlError("invalid_argument", f"unexpected parameters for {operation}")
    if operation == "set":
        if params.get("voltage") is None and params.get("current") is None:
            raise ControlError("invalid_argument", "provide voltage and/or current")
        for name in ("voltage", "current"):
            value = params.get(name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (float, int)) or
                                      not math.isfinite(value) or value < 0):
                raise ControlError("invalid_argument", f"{name} must be finite and nonnegative")
    if operation == "output" and type(params.get("enabled")) is not bool:
        raise ControlError("invalid_argument", "enabled must be a boolean")
    if operation == "read" and (type(params.get("register")) is not int or not 0 <= params["register"] <= 255):
        raise ControlError("invalid_argument", "register must be an integer in 0..255")
    tighter(params.get("max_voltage"))


class Controller:
    def __init__(self, device, store):
        self.device = device
        self.store = store

    def execute(self, operation: str, params: dict) -> dict:
        validate_request(operation, params)
        if operation in WRITES and not (operation == "output" and params["enabled"] is False):
            if self.store.report()["connection"]["state"] != "connected":
                raise ControlError("stale", "fresh device state is required before editing or enabling output")
        original_cap = self.device.max_voltage
        self.device.max_voltage = tighter(self.store.max_voltage, params.get("max_voltage"))
        attempts = self.device.write_attempts
        try:
            extra = {}
            if operation == "status":
                self.store.update(self.device.snapshot())
            elif operation == "info":
                extra = self.device.info()
            elif operation == "read":
                frame = self.device.read_register(params["register"])
                extra = {"register": f"0x{frame.register:02x}", "payload_hex": frame.payload.hex(" "),
                         **decode_payload(frame.register, frame.payload)}
            elif operation == "set":
                self.store.update(self.device.configure(voltage=params.get("voltage"), current=params.get("current")))
            elif operation == "output":
                self.store.update(self.device.set_output(params["enabled"]))
            elif operation == "lock":
                self.store.update(self.device.snapshot())
                self.store.panel_lock_requested = True
            elif operation == "unlock":
                self.device.close(keep_locked=False)
                self.store.panel_lock_requested = False
            return {**self.store.report(), **extra}
        except Exception as exc:
            if self.device.write_attempts != attempts:
                raise ControlError("uncertain_write", f"{exc}; the write may have taken effect. Read status before retrying.",
                                   uncertain=True) from exc
            raise control_error(exc) from exc
        finally:
            self.device.max_voltage = original_cap
