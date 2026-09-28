"""Private, versioned Unix-socket RPC. Only failed connection attempts mean absent."""
from concurrent.futures import TimeoutError as FutureTimeout
import fcntl
import json
import math
import os
from pathlib import Path
import queue
import select
import socket
import socketserver
import stat
import threading
import time
import uuid

from .control import WRITES
from .errors import ControlError, control_error
from .identity import Identity, runtime_dir

VERSION = 1
MAX_LINE = 65536


def encode_message(value: dict) -> bytes:
    return (json.dumps(value, allow_nan=False, separators=(",", ":")) + "\n").encode()


def decode_message(line: bytes) -> dict:
    if not line or len(line) > MAX_LINE or not line.endswith(b"\n"):
        raise ControlError("protocol", "missing, oversized or incomplete RPC message")
    def bad_constant(value):
        raise ValueError(f"invalid JSON number: {value}")
    try:
        message = json.loads(line, parse_constant=bad_constant)
        if not isinstance(message, dict) or message.get("v") != VERSION:
            raise ValueError("unsupported RPC version")
        return message
    except (ValueError, UnicodeError) as exc:
        raise ControlError("protocol", f"invalid RPC message: {exc}") from exc


class Ownership:
    def __init__(self, path: Path):
        self.path, self.fd = path, None

    def acquire(self):
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.close()
            raise ControlError("busy", "another process owns this device; no serial connection was opened") from exc
        return self

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.close()


class Endpoint:
    def __init__(self, identity: Identity, root: Path | None = None):
        self.identity = identity
        self.root = root if root is not None else runtime_dir()
        self.socket = self.root / f"{identity.key}.sock"
        self.metadata = self.root / f"{identity.key}.json"
        self.lock = self.root / f"{identity.key}.lock"


class ServiceAbsent(ControlError):
    def __init__(self):
        super().__init__("no_service", "no device service is listening")


class Client:
    def __init__(self, endpoint: Endpoint, timeout=15.0):
        self.endpoint, self.timeout = endpoint, timeout

    def _connect(self):
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(self.timeout)
        try:
            connection.connect(str(self.endpoint.socket))
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            connection.close()
            raise ServiceAbsent() from exc
        except Exception as exc:
            connection.close()
            raise control_error(exc) from exc
        return connection

    def _request(self, operation, params):
        return {"v": VERSION, "id": uuid.uuid4().hex, "op": operation,
                "params": params or {}, "timeout": self.timeout}

    def request(self, operation: str, params: dict | None = None) -> dict:
        request = self._request(operation, params)
        # Encode before connecting so invalid arguments cannot be sent.
        packet = encode_message(request)
        connection = self._connect()  # only this stage can raise ServiceAbsent
        try:
            with connection, connection.makefile("rb") as source:
                connection.sendall(packet)
                response = decode_message(source.readline(MAX_LINE + 1))
                if response.get("id") != request["id"]:
                    raise ControlError("protocol", "RPC response id mismatch")
                if "error" in response:
                    error = response["error"]
                    raise ControlError(error["code"], error["message"], uncertain=error.get("uncertain", False))
                return response["result"]
        except ControlError as exc:
            if exc.code == "protocol" and operation in WRITES:
                raise ControlError("uncertain_write", f"{exc}; inspect status before retrying", uncertain=True) from exc
            raise
        except (OSError, ValueError, KeyError, TypeError) as exc:
            uncertain = operation in WRITES
            raise ControlError("uncertain_write" if uncertain else "rpc_error",
                f"service connection failed: {exc}; no direct-access retry was attempted",
                uncertain=uncertain) from exc

    def events(self, kinds=("state",)):
        connection = self._connect()
        request = self._request("subscribe", {"events": list(kinds)})
        try:
            with connection, connection.makefile("rb") as source:
                connection.sendall(encode_message(request))
                while True:
                    event = decode_message(source.readline(MAX_LINE + 1))
                    if "error" in event:
                        error = event["error"]
                        raise ControlError(error["code"], error["message"], uncertain=error.get("uncertain", False))
                    yield event
        except OSError as exc:
            raise ControlError("rpc_error", f"event connection lost: {exc}") from exc


def known_services(root: Path | None = None) -> list[Identity]:
    root = root if root is not None else runtime_dir()
    identities = []
    for metadata in root.glob("*.json"):
        try:
            identity = Identity(**json.loads(metadata.read_text()))
            endpoint = Endpoint(identity, root)
            with Ownership(endpoint.lock):
                continue  # stale record; the next owner cleans it up under lock
        except ControlError as exc:
            if exc.code != "busy":
                raise
            identities.append(identity)
        except (OSError, ValueError, TypeError) as exc:
            raise ControlError("protocol", f"invalid service metadata {metadata}: {exc}") from exc
    return identities


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(5)
        request_id = None
        future = None
        try:
            request = decode_message(self.rfile.readline(MAX_LINE + 1))
            request_id = request.get("id")
            operation, params = request.get("op"), request.get("params", {})
            if not isinstance(request_id, str) or not isinstance(operation, str) or not isinstance(params, dict):
                raise ControlError("protocol", "RPC requires string id/op and object params")
            worker = self.server.worker
            if operation == "subscribe":
                self.subscribe(params)
                return
            if operation == "describe":
                if params:
                    raise ControlError("invalid_argument", "describe takes no parameters")
                result = {**worker.store.report(), "service": {"pid": os.getpid(), "version": VERSION}}
            else:
                timeout = request.get("timeout", 15)
                if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
                    raise ControlError("invalid_argument", "RPC timeout must be in (0, 60]")
                future = worker.submit(operation, params, timeout)
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        result = future.result(timeout=0.05)
                        break
                    except FutureTimeout:
                        if self.peer_closed():
                            future.cancel()
                            return
                        if time.monotonic() >= deadline:
                            cancelled = future.cancel()
                            uncertain = operation in WRITES and not cancelled
                            raise ControlError("uncertain_write" if uncertain else "timeout",
                                "request timed out; no automatic retry was made", uncertain=uncertain)
            self.send({"id": request_id, "result": result})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            if future is not None:
                future.cancel()
        except Exception as exc:
            try:
                self.send({"id": request_id, "error": control_error(exc).as_dict()})
            except OSError:
                pass

    def peer_closed(self):
        ready, _, _ = select.select([self.connection], [], [], 0)
        return bool(ready) and not self.connection.recv(1, socket.MSG_PEEK)

    def send(self, data):
        self.connection.sendall(encode_message({"v": VERSION, **data}))

    def subscribe(self, params):
        kinds = params.get("events", ["state"])
        if params.keys() - {"events"} or not isinstance(kinds, list) or not kinds or any(
            item not in ("state", "trace") for item in kinds
        ):
            raise ControlError("invalid_argument", "subscription events must contain state and/or trace")
        worker = self.server.worker
        subscription = worker.store.bus.subscribe(set(kinds))
        self.connection.settimeout(1)
        try:
            self.send({"event": "ready", "data": worker.store.report()})
            while True:
                try:
                    event = subscription.events.get(timeout=0.5)
                except queue.Empty:
                    if worker.finished.is_set() or self.peer_closed():
                        return
                    self.send({"event": "state" if "state" in kinds else "heartbeat",
                               "data": worker.store.report() if "state" in kinds else {}})
                    continue
                if event["event"] == "error":
                    self.send({"error": event["data"]})
                    return
                self.send(event)
                if event["event"] == "state" and event["data"]["connection"]["state"] == "stopped":
                    return
        finally:
            worker.store.bus.unsubscribe(subscription)


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 64


class Service:
    def __init__(self, worker, endpoint: Endpoint):
        self.worker, self.endpoint = worker, endpoint
        self.ownership = Ownership(endpoint.lock)
        self.server = None
        self.thread = self.monitor = None
        self._close_lock = threading.Lock()
        self._closed = False

    def start(self):
        self.ownership.acquire()
        try:
            path = self.endpoint.socket
            if path.exists():
                if not stat.S_ISSOCK(path.lstat().st_mode):
                    raise ControlError("permission", f"refusing to replace non-socket {path}")
                path.unlink()
            self.server = _Server(str(path), _Handler)
            os.chmod(path, 0o600)
            self.server.worker = self.worker
            self.endpoint.metadata.write_text(json.dumps(self.worker.identity.as_dict()))
            os.chmod(self.endpoint.metadata, 0o600)
            self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1},
                                           name="dps150-rpc", daemon=True)
            self.thread.start()
            self.worker.start()
            self.monitor = threading.Thread(target=self._monitor, name="dps150-owner", daemon=True)
            self.monitor.start()
            return self
        except BaseException:
            if self.server is not None:
                self.server.server_close()
            self.ownership.close()
            raise

    def _monitor(self):
        self.worker.finished.wait()
        self.close()

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self.worker.stop()
            self.worker.join()
            if self.thread is not None:
                self.server.shutdown()
                self.thread.join()
            if self.server is not None:
                self.server.server_close()
            self.endpoint.socket.unlink(missing_ok=True)
            self.endpoint.metadata.unlink(missing_ok=True)
            self.ownership.close()
