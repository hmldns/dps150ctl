"""Stable machine-readable failures, shared by direct access and RPC."""
from .protocol import DPS150Error, ProtocolError


class ControlError(DPS150Error):
    def __init__(self, code: str, message: str, *, uncertain: bool = False):
        super().__init__(message)
        self.code = code
        self.uncertain = uncertain

    def as_dict(self) -> dict:
        return {"code": self.code, "message": str(self), "uncertain": self.uncertain}


def control_error(exc: Exception) -> ControlError:
    if isinstance(exc, ControlError):
        return exc
    message = str(exc)
    if isinstance(exc, PermissionError) or "Permission denied" in message:
        return ControlError("permission", f"{message}. Grant access to the serial device; see README.")
    if isinstance(exc, (ValueError, TypeError)):
        return ControlError("invalid_argument", message)
    if isinstance(exc, ProtocolError):
        return ControlError("protocol", message)
    if "exclusively lock" in message or "Resource temporarily unavailable" in message:
        return ControlError("busy", message)
    if "timeout" in message.lower() or isinstance(exc, TimeoutError):
        return ControlError("timeout", message)
    return ControlError("device_error", message)
