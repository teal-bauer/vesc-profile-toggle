#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 teal-bauer
"""Profile toggle: identify, read, apply RAM limits, verify.

Two gestures qualify:
  * stationary - ready to drive, both brakes held, no speed; the seat button
    toggles between the two saved presets.
  * in motion - ready to drive at MOVING_MIN_KMH or more; the seat button
    toggles too, but a switch whose speed limit would fall below the current
    speed is refused instead of commanding regen.

The rule alone qualifies neither gesture; this helper reads the datastore and
decides, so the same seat-button topic can carry both. Saved state is advisory;
every toggle reads the ECU.
"""
import fcntl
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

from vesc_profile_io import (CONFIG_KEYS, PROFILES, SocketTransport, Session,
                             classify_limits, load_profile)

LABELS = {'default': 'Default', 'limited': '35 km/h limit', 'neither': 'unrecognized limits'}

# Ready-to-drive speed at or above which the seat button may switch in motion.
# Keep this below the lower profile's cap so the switch is actually reachable.
MOVING_MIN_KMH = 25.0
# l_current_min_scale, and the input-current limits the battery pushes over
# CAN, are recomputed by the ECU and are not part of a saved preset.
DYNAMIC_KEYS = ('current_min_scale', 'battery_current_min', 'battery_current_max')
# Geometry identifies the vehicle; none of the presets changes it.
GEOMETRY_KEYS = ('poles', 'gear_ratio', 'wheel_diameter_m')
# Every field read back, for diagnostics only.
REPORTED_KEYS = CONFIG_KEYS + DYNAMIC_KEYS + GEOMETRY_KEYS


class ToggleError(Exception):
    pass


def differing(first, second, keys):
    """Names whose values differ beyond the preset comparison tolerance."""
    result = []
    for key in keys:
        try:
            left, right = first[key], second[key]
        except KeyError:
            result.append(key)
            continue
        if not (isinstance(left, (int, float)) and isinstance(right, (int, float))
                and math.isfinite(left) and math.isfinite(right)
                and math.isclose(left, right, rel_tol=1e-5, abs_tol=1e-5)):
            result.append(key)
    return result


class Runtime:
    def __init__(self):
        self.directory = Path(os.environ.get('VESC_PROFILE_STATE_DIR', '/run/vesc-profile-toggle'))
        self.frames = Path(os.environ.get('VESC_PROFILE_FRAMES', '/data/extensions/vesc-profiles'))
        self.redis = os.environ.get('VESC_REDIS_CLI', 'redis-cli')
        self.interface = os.environ.get('VESC_CAN_INTERFACE', 'can0')

    def command(self, *args):
        result = subprocess.run([self.redis, '--raw', *args], capture_output=True,
                                text=True, timeout=0.4, check=True)
        return result.stdout.rstrip('\n')

    def hmget(self, hash, *fields):
        text = self.command('HMGET', hash, *fields)
        values = text.split('\n') if text else []
        values += [''] * (len(fields) - len(values))
        return values[:len(fields)]

    def speed(self):
        value = self.hmget('engine-ecu', 'speed')[0]
        try:
            kmh = float(value)
        except ValueError:
            return None
        return kmh if math.isfinite(kmh) else None

    def mode(self):
        """'stationary', 'moving', or None when the press does not qualify."""
        state, power, left, right = self.hmget('vehicle', 'state', 'engine-power',
                                               'brake:left', 'brake:right')
        speed, rpm = self.hmget('engine-ecu', 'speed', 'rpm')
        if (state == 'ready-to-drive' and power == 'on' and left == 'on'
                and right == 'on' and speed == '0' and rpm == '0'):
            return 'stationary'
        if state == 'ready-to-drive':
            kmh = self.speed()
            if kmh is not None and kmh >= MOVING_MIN_KMH:
                return 'moving'
        return None

    def ready(self):
        state, power, left, right = self.hmget('vehicle', 'state', 'engine-power',
                                               'brake:left', 'brake:right')
        speed, rpm = self.hmget('engine-ecu', 'speed', 'rpm')
        return (state == 'ready-to-drive' and power == 'on' and left == 'on'
                and right == 'on' and speed == '0' and rpm == '0')

    def power(self):
        return self.command('HGET', 'vehicle', 'engine-power')

    def save(self, profile, stamp):
        temporary = self.directory / 'state.new'
        temporary.write_text(f'{profile} {stamp}\n')
        temporary.replace(self.directory / 'state')

    def previous(self):
        try:
            _, stamp = (self.directory / 'state').read_text().split()
            return int(stamp)
        except FileNotFoundError:
            return 0

    def notify(self, phase, title, body, severity='info'):
        # Separate stable IDs keep the three stages inspectable without an
        # unbounded stack of notices. Failure of this live-only UI is nonfatal.
        title = title.strip().encode('utf-16-le')[:240].decode('utf-16-le', errors='ignore')
        body = body.encode('utf-16-le')[:1024].decode('utf-16-le', errors='ignore')
        message = {'source': 'vesc-profile', 'id': phase, 'title': title,
                   'body': body, 'severity': severity, 'ttl_ms': 10000}
        try:
            self.command('PUBLISH', 'scootui:notification', json.dumps(message))
        except (OSError, subprocess.SubprocessError) as error:
            self.log(f'Notification unavailable ({type(error).__name__}); continuing')

    def log(self, text):
        print(text, file=sys.stderr, flush=True)
        try:
            subprocess.run(['logger', '-t', 'vesc-profile-toggle', text], timeout=0.3,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.SubprocessError):
            pass


def preset_cap_kmh(profile):
    return PROFILES[profile][3] * 3.6


def execute(runtime, stamp, transport_factory=SocketTransport, session_factory=Session):
    stage = 'trigger'
    attempted = False

    def mark_write():
        nonlocal attempted
        if not attempted:
            runtime.save('unknown', stamp)
            attempted = True

    try:
        mode = runtime.mode()
        if mode is None:
            runtime.log('Seat button press did not qualify for a profile change')
            return 0
        stage = 'ECU identification'
        runtime.notify('trigger', 'Profile toggle triggered', 'Reading ECU profile.')
        with transport_factory(runtime.interface) as transport:
            transport.write_guard = mark_write
            session = session_factory(transport)
            identity = session.identify()
            stage = 'initial readback'
            initial = session.read_limits()
            current = classify_limits(initial)
            runtime.notify('readback', 'ECU profile readback',
                           f'{identity["hardware"]}, CAN 70: {LABELS[current]}.',
                           'warning' if current == 'neither' else 'info')
            runtime.log(f'Identified {identity}; readback profile={current} (mode={mode})')
            if current == 'neither':
                runtime.save('unknown', stamp)
                raise ToggleError('ECU limits match neither saved profile; left unchanged')
            if mode == 'stationary' and initial['current_min_scale'] != 0.0:
                raise ToggleError('Standstill braking scale is not zero; left unchanged')
            runtime.save(current, stamp)
            desired = 'limited' if current == 'default' else 'default'
            packet = load_profile(runtime.frames / f'{desired}.frames')
            stage = 'pre-application readback'
            # A bridge/app can change settings between reads. Do not overwrite
            # a detected intervening preset change; simultaneous writers still
            # cannot be excluded by this unauthenticated CAN protocol.
            latest = session.read_limits()
            moved = differing(initial, latest, REPORTED_KEYS)
            if moved:
                runtime.log('ECU fields moved between reads: ' + ', '.join(moved))
            preset = differing(initial, latest, CONFIG_KEYS)
            if preset or classify_limits(latest) != current:
                runtime.save('unknown', stamp)
                raise ToggleError('ECU preset changed during operation (%s); no write attempted'
                                  % (', '.join(preset) or 'identity'))
            if mode == 'stationary':
                if not runtime.ready() or latest['current_min_scale'] != 0.0:
                    raise ToggleError('Scooter moved, brakes released or braking scale changed; no write attempted')
            else:
                kmh = runtime.speed()
                if runtime.mode() != 'moving' or kmh is None:
                    raise ToggleError('Scooter no longer qualifies for an in-motion switch; no write attempted')
                cap = preset_cap_kmh(desired)
                if cap < kmh:
                    runtime.save(current, stamp)
                    runtime.notify('application', 'Profile switch blocked',
                                   f'{kmh:.0f} km/h is above the {LABELS[desired]} limit '
                                   f'of {cap:.0f} km/h; kept {LABELS[current]}.', 'warning')
                    runtime.log(f'In-motion switch to {desired} refused: {kmh:.1f} km/h '
                                f'exceeds {cap:.1f} km/h cap')
                    return 0
            stage = 'application'
            runtime.notify('application', 'Applying profile', f'Applying {LABELS[desired]} in RAM only.')
            session.apply(packet, latest['current_min_scale'])
            stage = 'verification readback'
            observed = session.read_limits()
            if classify_limits(observed) != desired:
                raise ToggleError('Readback does not match the requested profile')
            for field in GEOMETRY_KEYS:
                if not math.isclose(initial[field], observed[field], rel_tol=1e-5, abs_tol=1e-5):
                    raise ToggleError(f'Unexpected change in {field}')
            runtime.save(desired, stamp)
            runtime.notify('readback', 'ECU readback verified',
                           f'{identity["hardware"]}, CAN 70: {LABELS[desired]} confirmed.', 'success')
            runtime.notify('application', 'Profile verified',
                           f'{LABELS[desired]} applied and confirmed by ECU readback.', 'success')
            runtime.log(f'Applied and verified {desired}; RAM only')
            return 0
    except Exception as error:
        suffix = 'Do not assume the profile changed.' if attempted else 'No profile write attempted.'
        runtime.log(f'Failed at {stage}: {error}. {suffix}')
        if 'readback' in stage or stage == 'ECU identification':
            runtime.notify('readback', 'ECU readback failed', str(error)[:512], 'warning')
        runtime.notify('application', 'Profile not verified' if attempted else 'Profile toggle skipped',
                       f'{error}. {suffix}'[:512], 'warning')
        return 1


def main():
    os.umask(0o077)
    runtime = Runtime()
    runtime.directory.mkdir(parents=True, exist_ok=True)
    with (runtime.directory / 'lock').open('w') as lock:
        deadline = time.monotonic() + 2
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ToggleError('Another profile operation is still running')
                time.sleep(0.05)
        if sys.argv[1:] == ['reset']:
            if runtime.power() != 'off':
                raise ToggleError('Manual reset requires ECU power off')
            runtime.save('unknown', 0)
            return 0
        raw_event = sys.stdin.buffer.read(65537)
        if len(raw_event) > 65536:
            raise ToggleError('Event exceeds byte limit')
        event = json.loads(raw_event)
        if not isinstance(event, dict):
            raise ToggleError('Event must be an object')
        stamp = event.get('ts')
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp <= 0 or stamp != int(stamp):
            raise ToggleError('Invalid event timestamp')
        stamp = int(stamp)
        topic = os.environ.get('LS_TOPIC', '')
        if topic in ('input.engine-power', 'vehicle.engine-power.changed'):
            if stamp <= runtime.previous():
                runtime.log(f'Skipped superseded power event {stamp}')
                return 0
            target = os.environ.get('LS_TO', '')
            if target not in ('on', 'off'):
                raise ToggleError('Invalid power event')
            if runtime.power() == target:
                runtime.save('unknown', stamp)
                runtime.log('Power changed; active profile must be read from ECU')
            return 0
        if topic != 'button.seatbox.press':
            raise ToggleError('Unsupported trigger')
        return execute(runtime, stamp)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        runtime = Runtime()
        runtime.log(f'Profile helper failed before execution: {error}')
        runtime.notify('application', 'Profile toggle skipped', str(error)[:512], 'warning')
        sys.exit(1)
