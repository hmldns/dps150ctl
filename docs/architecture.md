# Device ownership and local RPC

`dps150ctl tui` and `dps150ctl daemon run` host the same `DeviceWorker` and Unix
socket service. A dashboard can attach to an existing owner. One-shot commands
first discover a service; only a failed initial connection (missing/refused
socket) permits direct access, which must acquire the same per-device lock.
Neither an RPC timeout nor a disconnected service permits fallback.

The serial worker alone reads/writes its serial handle. Complete command
transactions are serialized in a bounded queue. Its frame callback retains
every unsolicited frame, including frames in the same read after a requested
reply. Full snapshots are polled approximately once per second, alongside the
device's native telemetry. A slow consumer never blocks this thread.

## Identity and runtime files

USB VID, PID and serial number determine the device key (a truncated SHA-256).
The current tty is an address, not the identity. `/dev/serial/by-id/…` paths
are accepted through `--port`. USB location and interface metadata are exposed
by `ports` and state reports. Every connection checks the protocol model.

Runtime files live in `$XDG_RUNTIME_DIR/dps150ctl`, or
`/tmp/dps150ctl-UID` when that environment variable is unset. The directory
must be owned by the current user with mode 0700. Socket, lock and metadata
files use mode 0600. A service's identity record remains discoverable when its
USB device is absent. Advisory process locking plus PySerial exclusive access
prevent cooperating processes from opening another serial connection.
Stale sockets/records are replaced only while holding the ownership lock.

Reconnection searches for the same VID/PID/serial, with 1, 2, 4, then 8 second
delays. It never replays writes. No stable USB serial means no automatic
reconnection. Multiple matching identities fail explicitly. A fresh model
check and snapshot must succeed before edits/output-on become available.

## RPC v1

Each connection carries one newline-delimited JSON request and response, or
one subscription. Maximum line size is 64 KiB. JSON NaN/infinity are rejected.
This is a local interface, with no TCP listener or remote authentication layer.

```json
{"v":1,"id":"example","op":"set","params":{"voltage":0.42,"current":0.671,"max_voltage":1.0},"timeout":15}
```

Replies contain `v`, matching `id`, and either `result` or `error`. Errors contain
`code`, `message`, and `uncertain`. The electrical-write operations are `set`
and `output`; clients must not retry them automatically after an uncertain
response. A disconnected client cancels a queued request when possible; a
transaction already running may finish. It is never repeated by the service.

Operations: `describe` (cached service/state metadata), `status` (fresh full
snapshot), `info`, `read`, `set`, `output`, `lock`, `unlock`, and `stop`.
`output` uses `{"enabled":true}` or `false`; `read` uses an integer `register`.
Unknown operations/parameters are rejected. There is no raw-write endpoint.

```json
{"v":1,"id":"watch","op":"subscribe","params":{"events":["state","trace"]}}
```

The first event is `ready`, with current state. Following events contain `v`,
`event`, and `data`. State subscribers get live updates and heartbeat state
reports; trace-only subscribers receive empty `heartbeat` events between raw
chunks. A slow subscription fails with `slow_consumer` instead of silently
discarding capture data. Captures belong to the requesting process: the server
does not accept client filesystem paths.

State retains original measurement fields and adds:

- `identity`: USB identifiers, serial, current port, location, interface.
- `connection`: state, sample age, last error, retry delay, generation.
- `sample_monotonic`: host monotonic sample time for plotting/freshness.
- `limits`: effective voltage/current maxima, configured voltage cap,
  and whether the existing voltage setpoint exceeds the cap.
- `panel_lock_requested` and `panel_lock_verified` (always false: no known readback).

`describe` additionally returns service PID and RPC version. Measurements are
stale after two seconds without a fresh `C3` or full-state measurement.
Temperature and capability updates do not refresh measurement age. Protection
flags describe the supply and are separate from communications status.

## Shutdown and limits

Shutdown closes the host handle without sending session-close, so it does not
change output and requests retention of the front-panel lock. Explicit `unlock`
sends session-close, preserves output, and stops a running owner. The low-level
`DPS150` context manager retains its original release-on-exit behavior; callers
can explicitly close with `keep_locked=True` before leaving the context.

The configured cap, owner startup cap, optional per-request cap, current device
capability, and nominal 30 V maximum combine by minimum. Current remains capped
by the reported capability and nominal 5 A. Arguments are all checked before
any SET; current precedes voltage; readback confirms completion. Output-on
checks the existing voltage setpoint first. Output-off remains permitted when
measurement data are stale but the transport is connected.

All execution uses these same controls, including dashboard requests and
direct fallback. These caps constrain this program's commands, not other
programs or hardware OVP. Existing over-cap output is reported, never silently
changed. There is no output-on, output-off, or settings restoration on startup,
shutdown, reconnect, or a read command.
