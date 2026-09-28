"""Thread-safe state and bounded subscribers; device I/O never waits on viewers."""
from copy import deepcopy
import queue
import threading
import time

from .protocol import GET, MEASUREMENTS, decode_payload, utc_now

STALE_SECONDS = 2.0


class Subscription:
    def __init__(self, kinds: set[str], size: int = 256):
        self.kinds = kinds
        self.events = queue.Queue(maxsize=size)
        self.closed = False


class EventBus:
    def __init__(self):
        self._lock = threading.Lock()
        self._subscribers: set[Subscription] = set()

    def subscribe(self, kinds: set[str]) -> Subscription:
        subscriber = Subscription(kinds)
        with self._lock:
            self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscription):
        with self._lock:
            self._subscribers.discard(subscriber)
        subscriber.closed = True

    def publish(self, kind: str, data: dict):
        event = {"event": kind, "data": data}
        with self._lock:
            for subscriber in list(self._subscribers):
                if kind not in subscriber.kinds:
                    continue
                try:
                    subscriber.events.put_nowait(event)
                except queue.Full:
                    self._subscribers.remove(subscriber)
                    subscriber.closed = True
                    # Never silently lose raw capture data. Fail the subscription.
                    while not subscriber.events.empty():
                        try:
                            subscriber.events.get_nowait()
                        except queue.Empty:
                            break
                    subscriber.events.put_nowait({"event": "error", "data": {
                        "code": "slow_consumer", "message": "subscription overflow; capture is incomplete",
                        "uncertain": False}})


class StateStore:
    def __init__(self, identity, max_voltage=None, bus=None):
        self.lock = threading.RLock()
        self.identity = identity
        self.max_voltage = max_voltage
        self.bus = bus or EventBus()
        self.values = {}
        self.sample_time = None
        self.connection = "connecting"
        self.last_error = None
        self.retry_at = None
        self.panel_lock_requested = False
        self.generation = 0

    def update(self, values: dict):
        with self.lock:
            self.values.update(values)
            if all(name in values for name in MEASUREMENTS):
                self.sample_time = time.monotonic()
                self.values["timestamp_utc"] = values.get("timestamp_utc", utc_now())
        self.publish()

    def frame(self, frame):
        if frame.command == GET:
            self.update(decode_payload(frame.register, frame.payload))

    def transition(self, state: str, *, error=None, retry_at=None):
        with self.lock:
            if state == "connected" and self.connection != "connected":
                self.generation += 1
            self.connection = state
            self.last_error = error
            self.retry_at = retry_at
        self.publish()

    def report(self) -> dict:
        with self.lock:
            age = None if self.sample_time is None else max(0, time.monotonic() - self.sample_time)
            status = self.connection
            if status == "connected" and (age is None or age > STALE_SECONDS):
                status = "stale"
            vcap = min(30.0, self.values.get("max_voltage_v", 30.0),
                       self.max_voltage if self.max_voltage is not None else 30.0)
            return deepcopy({**self.values, "identity": self.identity.as_dict(),
                "connection": {"state": status, "sample_age_s": age,
                    "last_error": self.last_error, "retry_in_s": None if self.retry_at is None else
                    max(0, self.retry_at - time.monotonic()), "generation": self.generation},
                "sample_monotonic": self.sample_time,
                "limits": {"max_voltage_v": vcap,
                    "max_current_a": min(5.0, self.values.get("max_current_a", 5.0)),
                    "configured_max_voltage_v": self.max_voltage,
                    "voltage_above_cap": self.values.get("set_voltage_v", 0) > vcap},
                "panel_lock_requested": self.panel_lock_requested,
                "panel_lock_verified": False})

    def publish(self):
        self.bus.publish("state", self.report())
