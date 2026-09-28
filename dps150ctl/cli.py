"""Command-line entry point and direct/service routing."""
import argparse
from contextlib import ExitStack
import csv
import json
import math
from pathlib import Path
import signal
import sys
import tempfile
import threading
import time

from . import __version__
from .config import Settings
from .control import Controller
from .driver import DPS150, ports
from .errors import ControlError, control_error
from .identity import Identity, select_identity
from .ipc import Client, Endpoint, Ownership, Service, ServiceAbsent, known_services
from .protocol import CSV_FIELDS, FrameParser, RX
from .state import StateStore
from .trace import TraceWriter, decode_trace
from .worker import DeviceWorker


def positive_float(text):
    value = float(text)
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError("must be positive and finite")
    return value


def register_number(text):
    value = int(text, 0)
    if not 0 <= value <= 255:
        raise argparse.ArgumentTypeError("register must be in 0..255")
    return value


def show(data, as_json=False):
    if as_json:
        print(json.dumps(data, indent=2, allow_nan=False))
    elif isinstance(data, list):
        for item in data:
            print(f"{item['port']}: {item['description']} serial={item['serial_number']}"
                  + (" [DPS-150 candidate]" if item["candidate"] else ""))
    else:
        for key, value in data.items():
            print(f"{key:25s} {value:.6g}" if isinstance(value, float) else f"{key:25s} {value}")


class DirectConnection:
    def __init__(self, identity, settings, *, timeout=2.0, rtscts=False, trace=None):
        self.identity = identity
        self.store = StateStore(identity, settings.max_voltage)
        self.ownership = Ownership(Endpoint(identity).lock)
        self.device = DPS150(identity.port, timeout=timeout, rtscts=rtscts, trace=trace,
                             on_frame=self.store.frame, max_voltage=settings.max_voltage,
                             keep_locked_on_error=True)
        self.controller = Controller(self.device, self.store)

    def __enter__(self):
        self.ownership.acquire()
        try:
            self.identity = self.identity.resolve()
            self.store.identity = self.identity
            self.device.port = self.identity.port
            self.device.open()
            self.store.panel_lock_requested = True
            self.device.snapshot()
            self.store.transition("connected")
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *args):
        try:
            self.device.close(keep_locked=True)
        finally:
            self.ownership.close()

    def request(self, operation, params=None):
        return self.controller.execute(operation, params or {})


class RemoteTrace:
    """Subscribe before the command; capture overflow/error is never silent."""
    def __init__(self, client, file):
        self.client, self.file = client, file
        self.stop = threading.Event()
        self.error = None
        self.count = 0
        self.parser = FrameParser(RX)

    def __enter__(self):
        self.events = self.client.events(("trace",))
        next(self.events)  # subscription ready before any command
        self.thread = threading.Thread(target=self._run, name="dps150-capture", daemon=True)
        self.thread.start()
        return self

    def _run(self):
        try:
            for event in self.events:
                if event.get("event") == "trace":
                    record = event["data"]
                    self.file.write(json.dumps(record, allow_nan=False) + "\n")
                    self.file.flush()
                    if record["direction"] == "rx":
                        self.count += len(self.parser.feed(bytes.fromhex(record["hex"])))
                if self.stop.is_set():
                    break
        except Exception as exc:
            self.error = exc
        finally:
            self.events.close()

    def __exit__(self, exc_type, *args):
        self.stop.set()
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise ControlError("capture_error", "capture did not stop promptly; output may be incomplete")
        if self.error is not None and exc_type is None:
            raise self.error


def parser():
    result = argparse.ArgumentParser(description="FNIRSI DPS-150 CLI, dashboard and local device service")
    result.add_argument("--version", action="version", version=f"dps150ctl {__version__}")
    result.add_argument("--device", help="USB serial number (stable across tty renumbering)")
    result.add_argument("--port", help="serial path (or DPS150_PORT)")
    result.add_argument("--timeout", type=positive_float, default=2.0, help="serial response timeout, seconds")
    result.add_argument("--rtscts", action="store_true")
    result.add_argument("--config", type=Path, help="configuration file (default: XDG config directory)")
    result.add_argument("--max-voltage", type=float, help="tighten the software voltage cap")
    result.add_argument("--trace", type=Path, help="raw TX/RX JSONL to a new file")
    result.add_argument("--keep-locked", action="store_true", help="compatibility option; now the default")
    sub = result.add_subparsers(dest="command", required=True)
    for name in ("ports", "info", "status", "lock", "unlock"):
        sub.add_parser(name).add_argument("--json", action="store_true")
    read = sub.add_parser("read", help="read a register; decimal or 0xNN")
    read.add_argument("register", type=register_number)
    read.add_argument("--json", action="store_true")
    stream = sub.add_parser("stream", help="fresh full snapshots as JSONL or CSV")
    stream.add_argument("--format", choices=("jsonl", "csv"), default="jsonl")
    stream.add_argument("--interval", type=positive_float, default=1.0)
    stream.add_argument("--count", type=int, default=0, help="0 means until Ctrl-C")
    capture = sub.add_parser("capture")
    capture.add_argument("--output", type=Path, required=True)
    capture.add_argument("--duration", type=positive_float, default=10)
    decode = sub.add_parser("decode", help="decode raw JSONL offline")
    decode.add_argument("path", type=Path)
    configure = sub.add_parser("set", help="change setpoints while preserving output state")
    configure.add_argument("--voltage", type=float)
    configure.add_argument("--current", type=float)
    configure.add_argument("--json", action="store_true")
    output = sub.add_parser("output")
    output.add_argument("state", choices=("on", "off"))
    output.add_argument("--json", action="store_true")
    tui = sub.add_parser("tui", help="dashboard; attaches to a service or owns the device")
    tui.add_argument("--demo", action="store_true", help="synthetic device, isolated from real hardware")
    tui.add_argument("--view", choices=("combined", "numbers", "charts"), default="combined")
    daemon = sub.add_parser("daemon", help="foreground headless service")
    daemon_sub = daemon.add_subparsers(dest="daemon_command", required=True)
    daemon_sub.add_parser("run")
    for name in ("status", "stop"):
        daemon_sub.add_parser(name).add_argument("--json", action="store_true")
    permissions = sub.add_parser("permissions", help="preview or install persistent Linux device access")
    permissions_sub = permissions.add_subparsers(dest="permissions_command", required=True)
    for name in ("show", "install"):
        command = permissions_sub.add_parser(name)
        command.add_argument("--user", help="Linux account (default: current user, or SUDO_USER under sudo)")
        command.add_argument("--json", action="store_true")
    return result


def make_worker(identity, args, settings, **kwargs):
    return DeviceWorker(identity, max_voltage=settings.max_voltage, timeout=args.timeout,
                        rtscts=args.rtscts, **kwargs)


def long_running(args, settings, identity=None):
    from .tui import Dashboard  # regular CLI commands don't import the UI

    with ExitStack() as stack:
        service = None
        if args.command == "tui" and args.demo:
            from .demo import DemoDevice
            root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="dps150-demo-")))
            identity = Identity(None, "demo")
            endpoint = Endpoint(identity, root)
            service = Service(make_worker(identity, args, settings, factory=DemoDevice, reconnect=False), endpoint).start()
            stack.callback(service.close)
            client = Client(endpoint)
        else:
            endpoint = Endpoint(identity)
            client = Client(endpoint, timeout=min(60, max(15, args.timeout * 6 + 3)))
            if args.command == "daemon":
                service = Service(make_worker(identity, args, settings), endpoint).start()
                stack.callback(service.close)
            else:
                try:
                    client.request("describe")
                except ServiceAbsent:
                    try:
                        service = Service(make_worker(identity, args, settings), endpoint).start()
                        stack.callback(service.close)
                    except ControlError as exc:
                        if exc.code != "busy":
                            raise
                        # Another owner won startup. Only attach; never open another port.
                        client.request("describe")
        if args.command == "tui":
            Dashboard(client, view=args.view, max_voltage=settings.max_voltage, demo=args.demo,
                      owns_service=service is not None).run()
        else:
            print(f"dps150ctl listening on {endpoint.socket}; Ctrl-C stops without changing output", flush=True)
            previous = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, lambda *_: service.worker.stop())
            try:
                while not service.worker.finished.wait(0.25):
                    pass
            finally:
                signal.signal(signal.SIGTERM, previous)


def run_command(connection, args, settings, *, recording=None):
    operation, params = args.command, {}
    if operation == "set":
        params = {"voltage": args.voltage, "current": args.current, "max_voltage": settings.max_voltage}
    elif operation == "output":
        params = {"enabled": args.state == "on", "max_voltage": settings.max_voltage}
    elif operation == "read":
        params = {"register": args.register}
    if operation == "stream":
        writer = csv.DictWriter(sys.stdout, CSV_FIELDS, extrasaction="ignore") if args.format == "csv" else None
        if writer:
            writer.writeheader()
        count = 0
        while not args.count or count < args.count:
            started = time.monotonic()
            state = connection.request("status")
            if writer:
                writer.writerow(state)
            else:
                print(json.dumps(state, allow_nan=False))
            sys.stdout.flush()
            count += 1
            if not args.count or count < args.count:
                time.sleep(max(0, args.interval - (time.monotonic() - started)))
    elif operation == "capture":
        info = connection.request("info")
        connection.request("status")
        if isinstance(connection, DirectConnection):
            count = sum(1 for _ in connection.device.frames(args.duration))
        else:
            deadline = time.monotonic() + args.duration
            while time.monotonic() < deadline:
                if recording.error is not None:
                    raise recording.error
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            count = recording.count
        show({"model": info.get("model"), "trace": str(args.output), "telemetry_frames": count})
    else:
        show(connection.request(operation, params), getattr(args, "json", False))


def main(argv=None) -> int:
    arg_parser = parser()
    args = arg_parser.parse_args(argv)
    try:
        if args.command == "ports":
            show(ports(), args.json)
            return 0
        if args.command == "decode":
            decode_trace(args.path)
            return 0
        if args.command == "permissions":
            from .permissions import install_rule, prepare_rule
            # Administrative setup must not read user control settings, discover
            # per-user sockets, or open the serial port (especially under sudo).
            rule = prepare_rule(serial_number=args.device, port=args.port, username=args.user)
            if args.permissions_command == "show":
                if args.json:
                    show({"rule": rule.text, "filename": rule.filename, "user": rule.username,
                          "uid": rule.uid, "usb_serial": rule.identity.serial_number}, True)
                else:
                    print(rule.text, end="")
            else:
                show(install_rule(rule), args.json)
            return 0
        settings = Settings.load(args.config, max_voltage=args.max_voltage)
        if args.command == "stream" and args.count < 0:
            raise ValueError("--count cannot be negative")
        if args.command == "unlock" and args.keep_locked:
            raise ValueError("unlock cannot be combined with --keep-locked")
        if args.command == "capture" and args.trace:
            raise ValueError("capture uses --output; do not also supply --trace")
        if args.command in ("tui", "daemon") and args.trace:
            raise ValueError("use a separate 'capture' command while the dashboard/service runs")
        if args.command == "tui" and args.demo:
            long_running(args, settings)
            return 0
        identity = select_identity(serial_number=args.device, port=args.port, known=known_services())
        endpoint = Endpoint(identity)
        client = Client(endpoint, timeout=min(60, max(15, args.timeout * 6 + 3)))
        if args.command == "tui" or (args.command == "daemon" and args.daemon_command == "run"):
            long_running(args, settings, identity)
        elif args.command == "daemon":
            result = client.request("describe" if args.daemon_command == "status" else "stop")
            show(result, args.json)
        else:
            try:
                client.request("describe")
                remote = True
            except ServiceAbsent:
                remote = False
            with ExitStack() as stack:
                trace_path = args.output if args.command == "capture" else args.trace
                file = stack.enter_context(trace_path.open("x")) if trace_path else None
                recording = None
                if remote:
                    connection = client
                    if file:
                        recording = stack.enter_context(RemoteTrace(client, file))
                else:
                    connection = stack.enter_context(DirectConnection(identity, settings, timeout=args.timeout,
                        rtscts=args.rtscts, trace=TraceWriter(file) if file else None))
                run_command(connection, args, settings, recording=recording)
        return 0
    except KeyboardInterrupt:
        return 130
    except (Exception,) as exc:
        error = control_error(exc)
        if getattr(args, "json", False):
            print(json.dumps({"error": error.as_dict()}, allow_nan=False))
        else:
            print(f"error [{error.code}]: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
