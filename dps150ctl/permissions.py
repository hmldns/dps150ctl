"""Packaged, explicit Linux device-access setup. Never opens the serial port."""
from dataclasses import dataclass
from importlib.resources import files
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

from .errors import ControlError
from .identity import Identity, select_identity
from .protocol import PID, VID

RULES_DIRECTORY = Path("/etc/udev/rules.d")
SETFACL = Path("/usr/bin/setfacl")


@dataclass(frozen=True)
class AccessRule:
    identity: Identity
    username: str
    uid: int
    text: str

    @property
    def filename(self) -> str:
        return f"70-dps150ctl-{self.identity.serial_number}-u{self.uid}.rules"


def prepare_rule(*, serial_number=None, port=None, username=None) -> AccessRule:
    """Discover via sysfs only; an explicit serial also works while unplugged."""
    if sys.platform != "linux":
        raise ControlError("unsupported", "udev permissions setup is supported on Linux")
    identity = select_identity(serial_number=serial_number, port=port)
    if (identity.vid, identity.pid) != (VID, PID):
        raise ControlError("identity_mismatch", "permissions setup requires a 2e3c:5740 USB device")
    serial_number = identity.serial_number
    # Restrict to literal values safe in both udev match expressions and filenames.
    # In particular, reject wildcard matching and udev's $/% substitutions.
    if not isinstance(serial_number, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", serial_number):
        raise ControlError("invalid_argument", "a literal USB serial containing letters, digits, '.', '_' or '-' is required")
    if username is None:
        username = os.environ.get("SUDO_USER") if os.geteuid() == 0 else pwd.getpwuid(os.getuid()).pw_name
    if not username:
        raise ControlError("invalid_argument", "specify --user with the regular Linux account that runs dps150ctl")
    try:
        account = pwd.getpwnam(username)
    except KeyError as exc:
        raise ControlError("invalid_argument", f"unknown Linux user: {username}") from exc
    if account.pw_uid == 0:
        raise ControlError("invalid_argument", "choose a regular user; root already has device access")
    template = files("dps150ctl").joinpath("data/70-dps150ctl.rules.in").read_text(encoding="utf-8")
    # Numeric ACL IDs avoid injecting an account name into udev's RUN arguments.
    text = template.replace("@SERIAL@", serial_number).replace("@UID@", str(account.pw_uid))
    return AccessRule(identity, account.pw_name, account.pw_uid, text)


def _run(arguments: list[str]) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ControlError("udev", f"{arguments[0]} failed: {exc}") from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ControlError("udev", f"{shlex.join(arguments)}: {detail}")
    return result


def _existing_rule_matches(destination: Path, text: str) -> bool:
    if destination.is_symlink():
        raise ControlError("conflict", f"refusing to replace a symlink: {destination}")
    if not destination.exists():
        return False
    if not destination.is_file() or destination.read_text(encoding="utf-8") != text:
        raise ControlError("conflict", f"refusing to overwrite a different existing rule: {destination}")
    return True


def _install_file(rule: AccessRule, destination: Path):
    """Publish a complete rule atomically, without overwriting other content."""
    if _existing_rule_matches(destination, rule.text):
        return
    destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                     prefix=".dps150ctl-", suffix=".tmp") as staged:
        staged.write(rule.text)
        staged.flush()
        os.fchmod(staged.fileno(), 0o644)
        try:
            os.link(staged.name, destination)
        except FileExistsError:
            # A concurrent installer may have published the same rule first.
            _existing_rule_matches(destination, rule.text)


def install_rule(rule: AccessRule) -> dict:
    """Install and apply only this device's rule. Requires an explicit sudo run."""
    if os.geteuid() != 0:
        command = shlex.join(["sudo", sys.executable, "-m", "dps150ctl", "--device",
                             rule.identity.serial_number, "permissions", "install", "--user", rule.username])
        raise ControlError("permission", f"installing a udev rule requires administrator privileges; run: {command}")
    if not os.access(SETFACL, os.X_OK):
        raise ControlError("missing_dependency", "install the acl package first; /usr/bin/setfacl is required")
    udevadm = shutil.which("udevadm")
    if udevadm is None:
        raise ControlError("missing_dependency", "udevadm is required for permissions setup")
    destination = RULES_DIRECTORY / rule.filename
    _existing_rule_matches(destination, rule.text)
    version = _run([udevadm, "--version"]).stdout.strip().split()[0]
    # `verify` was introduced in systemd 254. Older udev releases can still use
    # the bundled rule, whose inputs are validated before any file is written.
    if int(version) >= 254:
        with tempfile.TemporaryDirectory(prefix="dps150ctl-rule-") as directory:
            staged = Path(directory) / rule.filename
            staged.write_text(rule.text, encoding="utf-8")
            _run([udevadm, "verify", str(staged)])
    _install_file(rule, destination)
    try:
        _run([udevadm, "control", "--reload-rules"])
        try:
            current = rule.identity.resolve()
        except ControlError as exc:
            if exc.code != "not_found":
                raise
            current = None
        applied_to = None
        if current is not None and current.port is not None:
            syspath = _run([udevadm, "info", "--query=path", f"--name={current.port}"]).stdout.strip()
            if not syspath.startswith("/devices/") or "\n" in syspath or ".." in syspath.split("/"):
                raise ControlError("udev", f"unexpected sysfs device path: {syspath!r}")
            _run([udevadm, "trigger", "--action=change", "--settle", f"/sys{syspath}"])
            applied_to = current.port
    except ControlError as exc:
        raise ControlError(exc.code, f"rule installed at {destination}, but activation failed: {exc}. "
                           "Fix the reported error and rerun permissions install.") from exc
    return {"rule_file": str(destination), "user": rule.username, "uid": rule.uid,
            "usb_serial": rule.identity.serial_number, "applied_to": applied_to,
            "note": "Access will be restored on reconnect. Run the dashboard as your regular user."}
