# vesc-profile-toggle

A seat-button profile switcher for Librescoot, packaged as an event-service
extension. Two rules invoke a Python helper that talks to a VESC-based TUNU
controller over SocketCAN and switches between two saved RAM-only limit
presets.

> **Live motor-control interface.** A profile write changes current and speed
> limits, possibly on a moving vehicle. Test with the driven wheel off the
> ground. The example presets in this repository are illustrative; replace them
> with values valid for your controller before installing.

## Gestures

Both gestures fire on `button.seatbox.press`. The rule carries no `when`
condition: the helper reads the datastore and decides, so one topic carries both
paths.

| Gesture | Qualification | Effect |
| --- | --- | --- |
| Stationary | `vehicle.state == ready-to-drive`, engine power on, both brakes on, speed and rpm zero | Toggles between the two presets |
| In motion | `vehicle.state == ready-to-drive` and `engine-ecu.speed >= 25` km/h | Toggles, but never to a preset whose limit is below the current speed |

The example presets are `default` (52 km/h forward) and `limited` (35 km/h
forward). `MOVING_MIN_KMH` and the preset table in `src/vesc_profile_io.py` are
the two knobs to change; keep the in-motion threshold below the lower preset's
cap so the switch is reachable. When an in-motion switch would command a limit
below the current speed it is refused with a `scootui:notification` instead of a
write. A press that qualifies as neither gesture exits silently.

## Safety model

- The helper identifies the controller before any read. VESC 6.x and 7.x TUNU
  hardware are accepted; a UUID is pinned only when `VESC_PROFILE_UUID` is set.
- It requires the expected geometry (48 poles, gear 1, wheel 0.416 m) and
  refuses to write when the preset readback is not one of the saved presets.
- A second readback before the write detects an intervening preset change. Only
  the preset fields the packet overwrites are compared. The controller
  recalculates its standstill braking scale and the battery may push
  input-current limits over CAN (`CAN_PACKET_CONF_CURRENT_LIMITS_IN`); those
  move on their own, so they are ignored and logged when they change.
- The write preserves the controller's live braking scale verbatim rather than
  forcing a value, and sends a single validated 37-byte opcode-49 packet with
  the store flag clear (RAM only, no flash, no retries).
- Post-write readback must classify as the desired preset with unchanged
  geometry. An ACK alone is not success.
- Saved state under `/run/vesc-profile-toggle` is advisory; every toggle selects
  from a fresh readback. Engine-power events mark the cache unknown.
- The bus has no exclusive-writer lock or compare-and-swap. Do not run VESC Tool
  against the controller at the same time.

## Repository layout

```
bin/vesc-profile-toggle          executable shell entry point
src/vesc_profile_io.py           SocketCAN transport, packet validation, readback
src/vesc_profile_toggle.py       gesture qualification, selection, verification, notifications
rules/vesc-profile-toggle.toml   event-service rules
fixtures/                        example preset packets (tests and install)
tests/test_profile_toggle.py     unit tests, mocked CAN and Redis
tools/probe_reads.py             read-only consecutive-readback probe
```

The deployed layout mirrors `/data/extensions` on the MDB:

```
/data/extensions/vesc-profile-toggle.toml
/data/extensions/bin/vesc-profile-toggle
/data/extensions/bin/vesc_profile_io.py
/data/extensions/bin/vesc_profile_toggle.py
/data/extensions/vesc-profiles/default.frames
/data/extensions/vesc-profiles/limited.frames
```

## Requirements

- Librescoot MDB: `python3`, `redis-cli`, Valkey, SocketCAN `can0`,
  `librescoot-events.service`.
- Event producers for `button.seatbox.press`, `vehicle`, and `engine-ecu`.
- A TUNU controller on CAN node 70 (VESC 6.x or 7.x).
- The installed controller application must report zero braking-current scale
  at standstill.

## Configure the presets

Edit `PROFILES` in `src/vesc_profile_io.py`. Each entry is the 37-byte
non-persistent setup preset as eight floats:

```
(current_min_scale, current_max_scale, min_erpm_m_s, max_erpm_m_s,
 duty_min, duty_max, watt_min, watt_max)
```

The matching `.frames` file under `fixtures/` (and at
`/data/extensions/vesc-profiles/`) must byte-match the declared entry;
`load_profile` enforces this. The frame file is seven lines: six opcode-49
fragments on CAN id `0x546` (offset byte plus payload) and one process frame on
`0x746` carrying the length and CRC16 of the reassembled 37-byte packet. Use a
captured VESC Tool preset or generate the frames from the values you intend to
allow.

To pin one controller, set `VESC_PROFILE_UUID` to its 12-byte UUID in hex in the
event-service unit environment. Leave it unset to accept any TUNU 6.x/7.x.

## Deploy

```sh
make bundle                       # runs tests, writes dist/*.tar.gz with SHA256SUMS
scp dist/vesc-profile-toggle-*.tar.gz mdb:/tmp/
ssh mdb 'cd /tmp && tar xzf vesc-profile-toggle-*.tar.gz && \
  cp -a vesc-profile-toggle-*/data/extensions/. /data/extensions/ && \
  rm -rf /data/extensions/bin/__pycache__ && \
  systemctl restart librescoot-events.service'
```

`rules/*.toml` changes require an event-service restart. The helper scripts are
started fresh by each `exec`, so script-only changes need no restart.

## Verify

```sh
python3 -m unittest discover -s tests -v          # no hardware needed
```

On the MDB:

```sh
redis-cli --raw HGETALL extensions                # expect rules 2, failed 0
journalctl -t vesc-profile-toggle -n 20           # helper outcomes
```

`tools/probe_reads.py` reads opcodes 0 and 91 repeatedly and prints field-level
diffs, showing which live values move between reads. It never writes:

```sh
lsc diag engine on
scp tools/probe_reads.py mdb:/tmp/
ssh mdb 'python3 /tmp/probe_reads.py --interface can0'
lsc diag engine off
```

## Protocol notes

The helper speaks the VESC packet protocol directly on `can0`:

- opcode 0 (`COMM_FW_VERSION`) for identity,
- opcode 91 (`COMM_GET_MCCONF_TEMP`) for limits; despite the name, the response
  is the live `mc_configuration` subset (10 floats, poles, gear ratio, wheel
  diameter), including values the firmware recomputes at runtime,
- opcode 49 for a non-persistent setup preset, framed with a CRC16 process
  frame.

Notifications are published on `scootui:notification` with source
`vesc-profile` and stable phase IDs (`trigger`, `readback`, `application`).
Pub/Sub delivery is best-effort and does not prove dashboard display.

## License

Copyright (C) 2026 teal-bauer.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version. See [LICENSE](LICENSE) for the full text. This program is distributed
in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the
implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
