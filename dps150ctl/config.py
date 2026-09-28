"""Optional persistent software voltage cap. Flags may only tighten it."""
from dataclasses import dataclass
import math
import os
from pathlib import Path
import tomllib

from .errors import ControlError


def voltage_cap(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ControlError("invalid_argument", "max_voltage must be a finite nonnegative number")
    return float(value)


def tighter(*caps: float | None) -> float | None:
    values = [voltage_cap(v) for v in caps if v is not None]
    return min(values) if values else None


@dataclass(frozen=True)
class Settings:
    max_voltage: float | None = None

    @classmethod
    def load(cls, path: Path | None = None, *, max_voltage: float | None = None) -> "Settings":
        explicit = path is not None
        path = path or Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "dps150ctl/config.toml"
        config = {}
        if explicit or path.exists():
            with path.open("rb") as source:
                config = tomllib.load(source)
            unknown = config.keys() - {"max_voltage"}
            if unknown:
                raise ControlError("invalid_argument", f"unknown config keys: {', '.join(sorted(unknown))}")
        return cls(tighter(config.get("max_voltage"), max_voltage))
