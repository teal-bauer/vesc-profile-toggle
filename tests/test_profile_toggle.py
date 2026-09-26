# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 teal-bauer
import copy
import math
import os
from pathlib import Path
import struct
import sys
from unittest import mock
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'src'))

import vesc_profile_io as io
import vesc_profile_toggle as toggle

PROFILES = ROOT / 'fixtures'
POLES = 48
GEAR = 1.0
WHEEL = .416
FAKE_UUID = '00112233445566778899aabb'
FAKE_UUID_HEX = bytes.fromhex(FAKE_UUID)


def erpm_factor():
    return (POLES / 2) * 60 * GEAR / (WHEEL * math.pi)


def reply_bytes(name, scale=0.0, battery_min=0.0, battery_max=60.0):
    """Build a command-91 reply from a declared profile instead of shipping one."""
    values = (scale, io.PROFILES[name][1], io.PROFILES[name][2] * erpm_factor(),
              io.PROFILES[name][3] * erpm_factor(), io.PROFILES[name][4],
              io.PROFILES[name][5], io.PROFILES[name][6], io.PROFILES[name][7],
              battery_min, battery_max)
    return (bytes((91,)) + struct.pack('>10f', *values) + bytes((POLES,))
            + struct.pack('>2f', GEAR, WHEEL))


def limits(name='default', scale=0.0):
    return io.decode_limits(reply_bytes(name, scale))


def identity_transport(major, hardware, uuid=FAKE_UUID_HEX):
    class IdentityTransport:
        def wait_live(self):
            pass

        def request(self, op):
            return bytes((0, major, 1)) + hardware.encode() + b'\0' + uuid
    return IdentityTransport()


class FakeTransport:
    def __init__(self, *args):
        self.sent = []
        self.write_guard = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def send_packet(self, packet):
        if self.write_guard:
            self.write_guard()
        self.sent.append(io._validate_packet(packet))


class FakeSession:
    def __init__(self, transport, initial, after=None, on_read=None):
        self.transport = transport
        self.initial = initial
        self.after = after or initial
        self.on_read = on_read
        self.count = 0
        self.applied = False

    def identify(self):
        return {'major': 7, 'minor': 1, 'hardware': 'TUNU', 'uuid': FAKE_UUID}

    def read_limits(self):
        self.count += 1
        if self.on_read:
            self.on_read(self.count)
        return copy.copy(self.after if self.applied else self.initial)

    def apply(self, packet, braking_scale):
        session = io.Session(self.transport)
        session._identified = True
        session.apply(packet, braking_scale)
        self.applied = True


class FakeRuntime:
    interface = 'can0'
    frames = PROFILES

    def __init__(self, mode='stationary', ready=None, speed=0.0):
        self._mode = mode
        self.readiness = iter(ready or [True] * 8)
        self._speed = speed
        self.saved = []
        self.notifications = []
        self.logs = []

    def mode(self):
        return self._mode

    def speed(self):
        return self._speed

    def ready(self):
        return next(self.readiness)

    def save(self, profile, stamp):
        self.saved.append((profile, stamp))

    def notify(self, *args):
        self.notifications.append(args)

    def log(self, text):
        self.logs.append(text)


class ProfileTests(unittest.TestCase):
    def test_generated_reply_matches_declared_profiles(self):
        current = io.decode_limits(reply_bytes('default'))
        self.assertEqual(current['current_min_scale'], 0)
        self.assertEqual(io.classify_limits(current), 'default')
        self.assertAlmostEqual(current['forward_m_s'] * 3.6, 52, places=3)
        limited = io.decode_limits(reply_bytes('limited'))
        self.assertEqual(io.classify_limits(limited), 'limited')
        self.assertAlmostEqual(limited['forward_m_s'] * 3.6, 35, places=3)

    def test_profile_files_match_allowed_packets(self):
        for name in ('default', 'limited'):
            packet = io.load_profile(PROFILES / f'{name}.frames')
            self.assertEqual(packet, bytes((49, 0, 0, 1, 0)) + struct.pack('>8f', *io.PROFILES[name]))
        self.assertEqual(io.classify_limits(limits('limited', .5)), 'limited')
        neither = limits('default')
        neither['forward_m_s'] = 30.0
        self.assertEqual(io.classify_limits(neither), 'neither')

    def test_packet_rejects_store_and_unapproved_values(self):
        packet = io.load_profile(PROFILES / 'limited.frames')
        for malformed in (packet[:1]+b'\x01'+packet[2:],
                          packet[:9]+b'\x00'*4+packet[13:],
                          packet[:5]+struct.pack('>f', float('nan'))+packet[9:],
                          packet[:5]+struct.pack('>f', 1.1)+packet[9:]):
            with self.assertRaises(io.ProtocolError):
                io._validate_packet(malformed)
        self.assertEqual(io._validate_packet(packet[:5]+struct.pack('>f', 0)+packet[9:])[5:9], b'\0'*4)

    def test_session_preserves_live_braking_scale(self):
        transport = FakeTransport()
        session = io.Session(transport)
        session._identified = True
        packet = io.load_profile(PROFILES / 'limited.frames')
        for scale, expected in ((0.0, b'\0'*4), (.25, struct.pack('>f', .25))):
            transport.sent.clear()
            session.apply(packet, scale)
            self.assertEqual(len(transport.sent), 1)
            self.assertEqual(transport.sent[0][5:9], expected)
            self.assertEqual(transport.sent[0][9:], packet[9:])
        for bad in (-.1, 1.1, float('nan')):
            with self.assertRaises(io.ProtocolError):
                session.apply(packet, bad)
        self.assertEqual(len(transport.sent), 1)

    def test_identity_accepts_tunu_6x_and_7x(self):
        for major, hardware in ((6, 'TUNU 606'), (7, 'TUNU')):
            io.Session(identity_transport(major, hardware)).identify()
        for major, hardware in ((5, 'TUNU'), (7, 'OTHER')):
            with self.assertRaises(io.ProtocolError):
                io.Session(identity_transport(major, hardware)).identify()

    def test_uuid_is_pinned_only_when_configured(self):
        os.environ.pop('VESC_PROFILE_UUID', None)
        io.Session(identity_transport(7, 'TUNU', uuid=b'\xff' * 12)).identify()
        with mock.patch.dict(os.environ, {'VESC_PROFILE_UUID': FAKE_UUID}):
            io.Session(identity_transport(7, 'TUNU')).identify()
            with self.assertRaises(io.ProtocolError):
                io.Session(identity_transport(7, 'TUNU', uuid=b'\xff' * 12)).identify()

    def test_runtime_modes_and_readiness(self):
        runtime = toggle.Runtime()
        replies = {'vehicle': 'ready-to-drive\non\non\non', 'engine-ecu': '0\n0'}
        runtime.command = lambda _, key, *args: replies[key]
        self.assertTrue(runtime.ready())
        self.assertEqual(runtime.mode(), 'stationary')
        replies['engine-ecu'] = '30\n900'
        self.assertFalse(runtime.ready())
        self.assertEqual(runtime.mode(), 'moving')
        self.assertEqual(runtime.speed(), 30.0)
        replies['engine-ecu'] = '10\n300'
        self.assertIsNone(runtime.mode())
        replies['vehicle'] = 'stand-by\non\non\non'
        replies['engine-ecu'] = '0\n0'
        self.assertIsNone(runtime.mode())

    def test_toggle_both_directions_preserves_braking_scale(self):
        for before, after in [('default', 'limited'), ('limited', 'default')]:
            runtime = FakeRuntime()
            transport = FakeTransport()
            session = FakeSession(transport, limits(before), limits(after))
            rc = toggle.execute(runtime, 123, lambda _: transport, lambda _: session)
            self.assertEqual(rc, 0, runtime.logs)
            self.assertEqual(runtime.saved[-1], (after, 123))
            self.assertEqual(len(transport.sent), 1)
            self.assertEqual(transport.sent[0][5:9], b'\0'*4)
            self.assertAlmostEqual(struct.unpack('>f', transport.sent[0][17:21])[0],
                                   io.PROFILES[after][3], places=4)

    def test_no_write_when_state_changes_or_scale_is_not_zero(self):
        cases = [(limits(scale=.25), [True]), (limits(), [False])]
        for initial, ready in cases:
            runtime = FakeRuntime(ready=ready)
            transport = FakeTransport()
            session = FakeSession(transport, initial, limits('limited'))
            self.assertEqual(toggle.execute(runtime, 123, lambda _: transport, lambda _: session), 1)
            self.assertEqual(transport.sent, [])

    def test_non_qualifying_press_is_silent(self):
        runtime = FakeRuntime(mode=None)
        transport = FakeTransport()
        session = FakeSession(transport, limits(), limits())
        self.assertEqual(toggle.execute(runtime, 123, lambda _: transport, lambda _: session), 0)
        self.assertEqual(transport.sent, [])
        self.assertEqual(runtime.notifications, [])

    def test_moving_switch_respects_the_lower_cap(self):
        runtime = FakeRuntime(mode='moving', speed=30.0)
        transport = FakeTransport()
        session = FakeSession(transport, limits('limited'), limits('default'))
        self.assertEqual(toggle.execute(runtime, 123, lambda _: transport, lambda _: session), 0)
        self.assertEqual(runtime.saved[-1], ('default', 123))
        self.assertEqual(len(transport.sent), 1)

        runtime = FakeRuntime(mode='moving', speed=40.0)
        transport = FakeTransport()
        session = FakeSession(transport, limits('default'), limits('limited'))
        self.assertEqual(toggle.execute(runtime, 123, lambda _: transport, lambda _: session), 0)
        self.assertEqual(transport.sent, [])
        self.assertTrue(any('blocked' in n[1] for n in runtime.notifications))

    def test_ignores_dynamic_fields_but_refuses_preset_change(self):
        runtime, transport = FakeRuntime(), FakeTransport()
        session = FakeSession(transport, limits(), limits('limited'))
        original = session.read_limits
        def dynamic_read():
            result = original()
            if session.count == 2:
                result['battery_current_min'] = -3.0
            return result
        session.read_limits = dynamic_read
        self.assertEqual(toggle.execute(runtime, 123, lambda _: transport, lambda _: session), 0)
        self.assertEqual(len(transport.sent), 1)

        runtime, transport = FakeRuntime(), FakeTransport()
        session = FakeSession(transport, limits(), limits('limited'))
        original = session.read_limits
        def preset_read():
            result = original()
            if session.count == 2:
                result['forward_m_s'] = 30.0
            return result
        session.read_limits = preset_read
        self.assertEqual(toggle.execute(runtime, 123, lambda _: transport, lambda _: session), 1)
        self.assertEqual(transport.sent, [])

    def test_verify_fails_without_confirmed_target(self):
        runtime, transport = FakeRuntime(), FakeTransport()
        session = FakeSession(transport, limits(), limits())
        self.assertEqual(toggle.execute(runtime, 123, lambda _: transport, lambda _: session), 1)
        self.assertEqual(runtime.saved[-1], ('unknown', 123))
        self.assertEqual(len(transport.sent), 1)


if __name__ == '__main__':
    unittest.main()
