# DPS-150 protocol and local observations

This document separates observed wire behavior from interpretations supplied by
existing reverse-engineering work. The local unit identified itself as DPS-150,
hardware V1.0, firmware V1.2, on 2026-09-27 (America/Los_Angeles).

## Transport and framing

Verified locally: USB CDC ACM, VID:PID `2e3c:5740`, 115200 baud, 8 data bits,
no parity, 1 stop bit. pyserial's default asserted RTS and DTR worked with
RTS/CTS flow control disabled. Commands are spaced at least 50 ms apart.
No baud-rate command was needed on this unit.

```text
header command register length payload[length] checksum
```

The header is `F1` toward the device and `F0` toward the host. Total frame size
is `5 + length`. The checksum is `(register + length + sum(payload)) & 0xff`;
header and command are excluded. Multi-byte floats are IEEE-754 binary32,
little-endian. Strings use the declared packet length and may contain a NUL.
USB read boundaries have no relationship to frame boundaries: local captures
include one-byte fragments, split state blocks, and concatenated packets.

| Command byte | Meaning | Used here |
| --- | --- | --- |
| `A1` | GET / read response / pushed measurement | Yes |
| `B1` | SET register | Only explicit control commands |
| `C1` | Session control; register 0, byte 1=open / 0=close | Yes |
| `B0` | Select baud rate; byte 5 means 115200 | Documented elsewhere; unnecessary locally |
| `C0` | Enter firmware-update mode | Not implemented |

The command byte and register byte are separate namespaces: **register `C0`
reads input voltage; command `C0` enters firmware-update mode**. The latter is
not a normal reset, according to the
[Windows-app protocol analysis](https://github.com/nasheed-x/dps150_api/blob/main/PROTOCOL.md).

## Session and captured examples

The tool opens the serial port, sends session-open, reads model `DE`, then sends
the requested reads. Replies and unsolicited telemetry may be interleaved.
The `dps150ctl` CLI, dashboard, and headless service close the host handle without
session-close by default, requesting retention of the panel lock. Explicit
`unlock` sends session-close and also stops a running owner. The low-level
`DPS150` context manager retains its original release-on-exit behavior unless
explicitly closed first with `keep_locked=True`. No output control register is
written as part of opening, reading, or closing a connection.

The [device manual](https://manuals.plus/fnirsi/dps-150-portable-cnc-dc-power-supply-manual.pdf)
describes automatic front-panel locking in PC-control mode. Leaving the remote
session engaged requests that lock; sending `F1 C1 00 01 00 01` releases it.
There is no confirmed lock-status register. The CLI reports the request without
claiming hardware verification; the user must observe the lock icon/buttons.
The lock request on this unit was captured in `captures/front-panel-lock.jsonl`;
it contained session-open and GETs only, with the 0.42 V / 0.671 A settings and
output-on state retained. Persistence after host port close requires visual
confirmation: the user subsequently confirmed that the panel was locked after
the command exited on this V1.2 unit. No persistence across power/USB resets is
promised. `--keep-locked` remains accepted for compatibility and is now the CLI
default, including cleanup after errors and Ctrl-C.

Actual packets from this unit:

```text
TX open session:  F1 C1 00 01 01 02
TX read model:    F1 A1 DE 01 00 DF
RX model:         F0 A1 DE 07 44 50 53 2D 31 35 30 8F   (DPS-150)
TX read hardware: F1 A1 DF 01 00 E0
RX hardware:      F0 A1 DF 04 56 31 2E 30 C8            (V1.0)
TX read firmware: F1 A1 E0 01 00 E1
RX firmware:      F0 A1 E0 04 56 31 2E 32 CB            (V1.2)
TX read all:      F1 A1 FF 01 00 00
RX all prefix:    F0 A1 FF 8B ...                       (139-byte payload)
TX close session: F1 C1 00 01 00 01
```

GET requests use a **one-byte zero payload**. The parser accepts only complete,
checksum-valid frames, rejects impossible lengths for known registers, and
retains incomplete frames until the next chunk. It reports unknown registers
as raw hex. Header/command bytes are not checksum-protected by this protocol.

## Register map

All 36 individual GET addresses from `C0` through `E3` returned valid responses
on this V1.2 unit, including presets, protection thresholds, and metering.
Register `E1` returned 1. The complete response log is in
`captures/register-report.json`; no register-range write probing was done.

| Register | Payload | Meaning |
| --- | --- | --- |
| `C0` | float32 | Input V |
| `C1` | float32 | Voltage setpoint V |
| `C2` | float32 | Current limit A |
| `C3` | 3 × float32 | Measured output V, A, W |
| `C4` | float32 | Temperature °C |
| `C5`–`D0` | float32 each | M1–M6 voltage/current pairs |
| `D1`–`D5` | float32 each | OVP V, OCP A, OPP W, OTP °C, LVP V |
| `D6` | byte | Brightness |
| `D7` | byte | Volume |
| `D8` | byte | Metering flag; polarity unresolved |
| `D9` | float32 | Capacity Ah |
| `DA` | float32 | Energy Wh |
| `DB` | byte | Output: 0=off, 1=on |
| `DC` | byte | Protection code |
| `DD` | byte | Regulation: 0=CC, 1=CV |
| `DE` | string | Model |
| `DF` | string | Hardware version |
| `E0` | string | Firmware version |
| `E1` | byte | Device address, per protocol analysis |
| `E2` | float32 | Available maximum voltage |
| `E3` | float32 | Reported maximum current |
| `FF` | packed block | Full state |

Protection codes: 0=OK, 1=OVP, 2=OCP, 3=OPP, 4=OTP, 5=LVP, 6=REP.
Other values are preserved and displayed as unknown. The full map and meanings
agree with [cho45's original protocol implementation](https://github.com/cho45/fnirsi-dps-150/blob/master/dps-150.js).

## Full-state layout

Offsets count from the first **payload** byte, excluding the four-byte header.
Our response contains 139 payload bytes, or 144 bytes including framing.

| Offset (decimal) | Type | Field |
| --- | --- | --- |
| 0 | float32 | Input voltage |
| 4, 8 | float32 each | Voltage/current setpoints |
| 12, 16, 20 | float32 each | Measured output V/A/W |
| 24 | float32 | Temperature |
| 28–72, every 4 bytes | 12 × float32 | Six preset V/A pairs |
| 76, 80, 84, 88, 92 | float32 each | OVP/OCP/OPP/OTP/LVP thresholds |
| 96, 97, 98 | byte each | Brightness, volume, metering flag |
| 99, 103 | float32 each | Capacity Ah, energy Wh |
| 107, 108, 109 | byte each | Output state, protection, CV/CC |
| 110 | byte | Unconfirmed; retained as `reserved_110_raw` |
| 111, 115 | float32 each | Available voltage/current maximums |
| 119, 123, 127, 131, 135 | float32 each | OVP/OCP/OPP/OTP/LVP ceiling values |

This is a packed structure: the floats starting at 99 and 111 are **unaligned**.
The decoder accepts the 119-byte core, decodes complete optional ceiling fields,
and preserves a trailer beyond byte 138. Support for shorter/extended firmware
variants is defensive; only the 139-byte layout has been verified on hardware.

The ceiling interpretation comes from the
[Windows-app analysis](https://github.com/nasheed-x/dps150_api/blob/main/PROTOCOL.md#7-full-state-dump-register-0xff).
Observed values were 30 V, 5.1 A, 150 W, 99 °C, and 30 V, consistent with that
interpretation. We did not change thresholds to establish their semantics.

## Observations and uncertainty

- An eight-second passive telemetry window, after session-open and initial
  reads, produced 75 valid frames: 15 each for `C3`, `C0`, `E2`, `E3`, and `C4`.
  Mean `C3` spacing was 0.525 seconds. No checksum failures, discarded bytes,
  or trailing partial frames were found in replay.
- Output was off. Initial input was 4.98985 V, temperature 29.7405 °C,
  setpoints 3.39940 V and 1 A, and protection code 5 (LVP). The LVP threshold
  was 5 V; the voltage capability was 4.78985 V, about 0.2 V below input.
  That relationship is an observation for this operating point, not a general
  capability formula imposed by the driver.
- The current capability reports 5.1 A. The control API caps requests at the
  [manufacturer's nominal 5 A rating](https://www.fnirsi.com/products/dps-150).
- The metering byte is 1. Sources disagree about whether the readback is
  inverted relative to the write flag. We expose `metering_raw`, without a
  guessed boolean or a metering-write API.
- Byte 110 is 1. Its identity remains unconfirmed. The driver does not assume
  it is the separately readable device address.
- A subsequent user-authorized output-on test sent `F1 B1 DB 01 01 DD`.
  The immediate snapshot reported output enabled and 0.24765 V during startup.
  After closing and reopening the session, six readings reported output enabled,
  3.39940 V, and zero measured current, with the existing 1 A limit. The output
  was left on as requested. No setpoint or protection writes were sent.
- Protection code 5 (LVP) persisted throughout that successful output-on test,
  at approximately 4.98 V input. This code alone does not prove output inhibition.
  Whether it is latched, describes a previous event, or has another qualification
  remains unresolved; automatic fault handling should not assume its semantics.
- CV/CC transitions, loaded operation, and metering behavior were not exercised.

## Implemented writes and verification boundary

The API implements SET `C1` (voltage), `C2` (current), and `DB` (output enable).
Encoding and simulated readback are tested. SET `C1`, `C2`, and `DB=1` were also
verified on the physical unit. A user-authorized change to 0.42 V / 0.67 A was
read back successfully, then output-on produced a settled 0.420 V reading after
reconnection, with protection status OK. SET `DB=0` has not been exercised on
hardware. Examples of expected packets:

```text
set 5.0 V: F1 B1 C1 04 00 00 A0 40 A5
set 1.0 A: F1 B1 C2 04 00 00 80 3F 85
output on: F1 B1 DB 01 01 DD
output off:F1 B1 DB 01 00 DC
```

Setpoint verification permits one nominal resolution step (0.01 V / 0.001 A)
plus a small float margin. A timeout after a write does not establish whether
the device applied it; the API raises an error and does not retry automatically.
Preset/protection writes and firmware update are intentionally outside the
current API. Register reads, capture, and offline decoding cover their data.

Fixtures: `tests/fixtures/v1.2-info.jsonl` and `tests/fixtures/v1.2-state.jsonl`.
Local working evidence: `captures/telemetry.jsonl`, `captures/readings.csv`, and
`captures/register-probe.jsonl`. These are serial application captures, not
USB bus-level traces.

Power-on proof: `captures/power-on-command.jsonl` records the write and immediate
readback; `captures/power-on-verify.jsonl` records the subsequent six samples.
`captures/power-on-proof.json` summarizes the verified command and last state.

Setpoint evidence: `captures/set-0.42v-0.67a.jsonl` records SET `C2` before SET
`C1` and before/after readbacks. `captures/power-on-0.42v-0.67a.jsonl` records
output-on; `captures/verify-0.42v-0.67a.jsonl` records the settled state.
