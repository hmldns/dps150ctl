"""The only thread allowed to touch an owned serial connection."""
from concurrent.futures import Future
from dataclasses import dataclass
import queue
import threading
import time

from .control import Controller
from .driver import DPS150
from .errors import ControlError, control_error
from .protocol import utc_now
from .state import StateStore


@dataclass
class Job:
    operation: str
    params: dict
    future: Future
    deadline: float


class DeviceWorker:
    def __init__(self, identity, *, max_voltage=None, timeout=2.0, rtscts=False,
                 factory=DPS150, reconnect=True):
        self.identity = identity
        self.store = StateStore(identity, max_voltage)
        self.timeout = timeout
        self.rtscts = rtscts
        self.factory = factory
        self.reconnect = reconnect and bool(identity.serial_number)
        self.jobs = queue.Queue(maxsize=64)
        self.stopping = threading.Event()
        self.finished = threading.Event()
        self.thread = threading.Thread(target=self._run, name="dps150-serial", daemon=True)
        self.device = None
        self.controller = None
        self.trace_start = time.monotonic()

    def start(self):
        self.thread.start()

    def submit(self, operation: str, params: dict, timeout=15.0) -> Future:
        future = Future()
        if self.stopping.is_set() or self.finished.is_set():
            future.set_exception(ControlError("stopped", "device owner has stopped"))
        elif self.controller is None and operation != "stop":
            future.set_exception(ControlError("disconnected", "device is not ready; command was not queued"))
        else:
            try:
                self.jobs.put_nowait(Job(operation, params, future, time.monotonic() + timeout))
            except queue.Full:
                future.set_exception(ControlError("busy", "command queue is full; no command was sent"))
        return future

    def stop(self):
        self.stopping.set()

    def join(self):
        if self.thread.ident is not None:
            self.thread.join()

    def trace(self, direction, data):
        self.store.bus.publish("trace", {"timestamp_utc": utc_now(),
            "elapsed_s": time.monotonic() - self.trace_start,
            "direction": direction, "hex": data.hex(" ")})

    def _disconnect(self, error, delay):
        if self.device is not None:
            try:
                self.device.close(keep_locked=True)
            except Exception:
                pass
        self.device = self.controller = None
        self._fail_pending(ControlError("disconnected", "connection lost; queued commands were not sent"))
        self.store.transition("reconnecting" if self.reconnect else "error", error=error.as_dict(),
                              retry_at=time.monotonic() + delay if self.reconnect else None)

    def _fail_pending(self, error):
        while True:
            try:
                job = self.jobs.get_nowait()
            except queue.Empty:
                break
            if job.future.set_running_or_notify_cancel():
                job.future.set_exception(error)

    def _job(self):
        try:
            job = self.jobs.get_nowait()
        except queue.Empty:
            return
        if not job.future.set_running_or_notify_cancel():
            return
        try:
            if time.monotonic() > job.deadline:
                raise ControlError("timeout", "command expired in queue; no command was sent")
            if job.operation == "stop":
                if job.params:
                    raise ControlError("invalid_argument", "stop takes no parameters")
                self.stopping.set()
                result = self.store.report()
            elif self.controller is None:
                raise ControlError("disconnected", "device is disconnected; command was not sent")
            else:
                result = self.controller.execute(job.operation, job.params)
                if job.operation == "unlock":
                    self.stopping.set()
            job.future.set_result(result)
        except Exception as exc:
            error = control_error(exc)
            job.future.set_exception(error)
            if error.code in {"device_error", "timeout", "protocol", "uncertain_write", "permission"}:
                self._disconnect(error, 1)

    def _run(self):
        delay, next_connect, next_snapshot, attempted = 1, 0.0, 0.0, False
        try:
            while not self.stopping.is_set():
                if self.device is None:
                    self._job()  # fail fast during reconnect; never queue writes across it
                    if self.stopping.is_set():
                        break
                    if time.monotonic() < next_connect or (attempted and not self.reconnect):
                        self.stopping.wait(0.05)
                        continue
                    self.store.transition("connecting", error=self.store.last_error)
                    attempted = True
                    try:
                        self.identity = self.identity.resolve()
                        self.store.identity = self.identity
                        self.device = self.factory(self.identity.port, timeout=self.timeout, rtscts=self.rtscts,
                            trace=self.trace, on_frame=self.store.frame, max_voltage=self.store.max_voltage,
                            keep_locked_on_error=True)
                        self.device.open()
                        self.store.panel_lock_requested = True
                        self.store.update(self.device.info())
                        self.store.update(self.device.snapshot())
                        self.controller = Controller(self.device, self.store)
                        self.store.transition("connected")
                        delay = 1
                        next_snapshot = time.monotonic() + 1
                    except Exception as exc:
                        self._disconnect(control_error(exc), delay)
                        next_connect = time.monotonic() + delay
                        delay = min(8, delay * 2)
                    continue
                self._job()
                if self.stopping.is_set():
                    break
                if self.device is None:
                    next_connect = time.monotonic() + 1
                    continue
                try:
                    self.device._receive()
                    if time.monotonic() >= next_snapshot:
                        self.store.update(self.device.snapshot())
                        next_snapshot = time.monotonic() + 1
                except Exception as exc:
                    self._disconnect(control_error(exc), 1)
                    next_connect = time.monotonic() + 1
        finally:
            if self.device is not None:
                try:
                    self.device.close(keep_locked=True)
                except Exception as exc:
                    self.store.last_error = control_error(exc).as_dict()
            self._fail_pending(ControlError("stopped", "device owner stopped; command was not sent"))
            self.store.transition("stopped", error=self.store.last_error)
            self.finished.set()
