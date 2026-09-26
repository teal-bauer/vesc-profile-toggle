# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 teal-bauer
"""Bounded classic-CAN profile I/O. Applying is send-only, never verification."""
import binascii
import math
import os
from pathlib import Path
import re
import select
import socket
import struct
import time


class ProtocolError(Exception):
    pass


EFF = 0x80000000
TARGET = 70
LOCAL = 254
FRAME = struct.Struct('=IB3x8s')
KEYS = ('current_min_scale', 'current_max_scale', 'min_erpm', 'max_erpm',
        'duty_min', 'duty_max', 'watt_min', 'watt_max',
        'battery_current_min', 'battery_current_max')
CONFIG_KEYS = ('current_max_scale', 'reverse_m_s', 'forward_m_s',
               'duty_min', 'duty_max', 'watt_min', 'watt_max')
# Example allowlist. Replace with the presets your controller accepts; the
# packet tail must match one of these exactly or the write is refused.
# Values are (current_min_scale, current_max_scale, min_erpm_m_s, max_erpm_m_s,
# duty_min, duty_max, watt_min, watt_max).
PROFILES = {
    'default': (1., 1., -1.9444444444444444, 14.444444444444445,
                .005, .95, -1500000., 1500000.),
    'limited': (1., 1., -1.9444444444444444, 35 / 3.6,
                .005, .95, -1500000., 1500000.),
}
_ALLOWED = tuple(bytes((49, 0, 0, 1, 0)) + struct.pack('>8f', *v)
                 for v in PROFILES.values())


def identity_matches(identity):
    """Accept a TUNU controller on VESC 6.x or 7.x.

    A UUID is only pinned when VESC_PROFILE_UUID is set, since the value is
    unique per controller and not part of a reusable configuration.
    """
    if not isinstance(identity, dict):
        return False
    if identity.get('major') not in (6, 7):
        return False
    if not str(identity.get('hardware', '')).startswith('TUNU'):
        return False
    required = os.environ.get('VESC_PROFILE_UUID', '').strip().lower()
    if required and str(identity.get('uuid', '')).lower() != required:
        return False
    return True


def crc16(packet):
    return binascii.crc_hqx(packet, 0)


def _validate_packet(packet):
    if not isinstance(packet, bytes) or len(packet) != 37 or packet[:5] != bytes((49, 0, 0, 1, 0)):
        raise ProtocolError('packet is not a nonpersistent setup preset')
    scale = struct.unpack('>f', packet[5:9])[0]
    if not math.isfinite(scale) or not 0 <= scale <= 1 or packet[9:] not in tuple(p[9:] for p in _ALLOWED):
        raise ProtocolError('packet contains unapproved limits')
    return packet


def decode_firmware(packet):
    if not isinstance(packet, bytes) or not 16 <= len(packet) <= 512 or packet[0] != 0:
        raise ProtocolError('invalid firmware response')
    end = packet.find(b'\0', 3)
    if end < 4 or end > 67 or len(packet) < end + 13:
        raise ProtocolError('missing or oversized hardware name / UUID')
    try:
        hardware = packet[3:end].decode('ascii')
    except UnicodeDecodeError as exc:
        raise ProtocolError('non-ASCII hardware name') from exc
    return {'major': packet[1], 'minor': packet[2], 'hardware': hardware,
            'uuid': packet[end + 1:end + 13].hex()}


def decode_limits(packet):
    if not isinstance(packet, bytes) or len(packet) != 50 or packet[0] != 91:
        raise ProtocolError('expected exact 50-byte opcode 91 response')
    values = struct.unpack('>10f', packet[1:41])
    poles = packet[41]
    gear, wheel = struct.unpack('>2f', packet[42:])
    if not all(math.isfinite(v) for v in (*values, gear, wheel)):
        raise ProtocolError('nonfinite limit or geometry')
    a, b, lo, hi, dl, dh, wl, wh, bl, bh = values
    if not (0 <= a <= 1 and 0 <= b <= 1 and -1e6 <= lo <= 0 <= hi <= 1e6
            and 0 <= dl <= dh <= 1 and -1.5e6 <= wl <= 0 <= wh <= 1.5e6
            and -1e4 <= bl <= 0 <= bh <= 1e4):
        raise ProtocolError('limits outside sane bounds')
    if not (2 <= poles <= 254 and poles % 2 == 0 and 0 < gear < 100
            and .05 < wheel < 2):
        raise ProtocolError('invalid geometry')
    factor = (poles / 2) * 60 * gear / (wheel * math.pi)
    result = dict(zip(KEYS, values))
    result.update(poles=poles, gear_ratio=gear, wheel_diameter_m=wheel,
                  reverse_m_s=lo / factor, forward_m_s=hi / factor)
    return result


def classify_limits(limits):
    try:
        if not (limits['poles'] == 48
                and math.isfinite(limits['current_min_scale'])
                and 0 <= limits['current_min_scale'] <= 1
                and math.isclose(limits['gear_ratio'], 1, rel_tol=1e-5, abs_tol=1e-5)
                and math.isclose(limits['wheel_diameter_m'], .416, rel_tol=1e-5, abs_tol=1e-5)):
            return 'neither'
        for name, values in PROFILES.items():
            if all(math.isfinite(limits[k]) and math.isclose(limits[k], v,
                   rel_tol=1e-5, abs_tol=1e-5) for k, v in zip(CONFIG_KEYS, values[1:])):
                return name
    except (KeyError, TypeError, ValueError):
        pass
    return 'neither'


def load_profile(path):
    """Parse data, never execute the legacy cansend file."""
    try:
        with Path(path).open('rb') as source:
            raw = source.read(4097)
        if len(raw) > 4096:
            raise ProtocolError('profile file exceeds byte limit')
        text = raw.decode('ascii')
    except (OSError, UnicodeError) as exc:
        raise ProtocolError('cannot read profile') from exc
    frames = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        tokens = line.split()
        if not 2 <= len(tokens) <= 9 or not re.fullmatch(r'0x[0-9a-fA-F]{1,8}', tokens[0]):
            raise ProtocolError('invalid frame line')
        if any(not re.fullmatch(r'0x[0-9a-fA-F]{2}', token) for token in tokens[1:]):
            raise ProtocolError('invalid frame byte')
        frames.append((int(tokens[0], 16), bytes(int(token, 16) for token in tokens[1:])))
    if len(frames) != 7:
        raise ProtocolError('expected six fragments and one process frame')
    packet = bytearray()
    for index, (can_id, data) in enumerate(frames[:6]):
        count = min(7, 37 - index * 7)
        if can_id != 0x546 or len(data) != count + 1 or data[0] != len(packet):
            raise ProtocolError('invalid profile fragment offset / length / ID')
        packet.extend(data[1:])
    expected = bytes((LOCAL, 0)) + struct.pack('>HH', 37, crc16(packet))
    if frames[6] != (0x746, expected):
        raise ProtocolError('invalid profile process frame / CRC')
    if bytes(packet) not in _ALLOWED:
        raise ProtocolError('profile file is not an exact saved preset')
    return bytes(packet)


class SocketTransport:
    def __init__(self, interface='can0'):
        self.interface = interface
        self._socket = None
        self.write_guard = None

    def __enter__(self):
        if self._socket is not None:
            raise ProtocolError('transport already open')
        sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        try:
            # ERR_FLAG is deliberately excluded from both filter masks.
            filters = struct.pack('=IIII', EFF | LOCAL, 0xc00000ff,
                                  EFF | (9 << 8) | TARGET, 0xdfffffff)
            sock.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER, filters)
            sock.setblocking(False)
            sock.bind((self.interface,))
        except BaseException:
            sock.close()
            raise
        self._socket = sock
        return self

    def __exit__(self, *args):
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def _open(self):
        if self._socket is None:
            raise ProtocolError('transport not open')
        return self._socket

    def _drain(self, deadline):
        sock = self._open()
        for _ in range(256):
            if time.monotonic() >= deadline:
                raise ProtocolError('receive deadline exceeded')
            try:
                sock.recv(72)
            except BlockingIOError:
                return
        raise ProtocolError('stale frame drain limit exceeded')

    def _recv(self, deadline):
        sock = self._open()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProtocolError('receive deadline exceeded')
            try:
                ready, _, _ = select.select([sock], [], [], remaining)
                if not ready:
                    raise ProtocolError('receive deadline exceeded')
                raw = sock.recv(72)
            except (BlockingIOError, InterruptedError):
                continue
            if len(raw) != 16 or raw[4] > 8:
                raise ProtocolError('invalid classic CAN frame')
            can_id, size, data = FRAME.unpack(raw)
            if can_id & 0xe0000000 != EFF:
                return None, b''
            return can_id & 0x1fffffff, data[:size]

    def _send(self, kind, data):
        raw = FRAME.pack(EFF | (kind << 8) | TARGET, len(data), data)
        try:
            if self._open().send(raw) != len(raw):
                raise ProtocolError('short CAN write; not retried')
        except OSError as exc:
            raise ProtocolError('CAN write failed; not retried') from exc

    def wait_live(self):
        deadline = time.monotonic() + 1.0
        self._drain(deadline)
        for _ in range(256):
            can_id, data = self._recv(deadline)
            if can_id == (9 << 8) | TARGET and len(data) == 8:
                return
        raise ProtocolError('liveness frame budget exceeded')

    def request(self, op):
        if type(op) is not int or op not in (0, 91):
            raise ProtocolError('only read-only opcodes 0 and 91 permitted')
        deadline = time.monotonic() + .75
        self._drain(deadline)
        self._send(8, bytes((LOCAL, 0, op)))
        packet = bytearray()
        for _ in range(256):
            can_id, data = self._recv(deadline)
            if can_id is None or can_id & 255 != LOCAL:
                continue
            kind = can_id >> 8
            if kind in (5, 6):
                head = 1 if kind == 5 else 2
                if len(data) <= head:
                    raise ProtocolError('short fragment')
                offset = int.from_bytes(data[:head], 'big')
                if offset != len(packet) or len(packet) + len(data) - head > 512:
                    raise ProtocolError('fragment offset / capacity mismatch')
                packet.extend(data[head:])
            elif kind == 7:
                if len(data) != 6 or data[:2] != bytes((TARGET, 1)):
                    raise ProtocolError('unexpected response sender or mode')
                size, crc = struct.unpack('>HH', data[2:])
                if not packet or size != len(packet) or crc != crc16(packet):
                    raise ProtocolError('response length / CRC mismatch')
                if packet[0] == op:
                    return bytes(packet)
                packet.clear()
            elif kind == 8:
                if len(data) < 3 or data[:2] != bytes((TARGET, 1)) or packet:
                    raise ProtocolError('unexpected short response')
                if data[2] == op:
                    return data[2:]
        raise ProtocolError('response frame budget exceeded')

    def send_packet(self, packet):
        packet = _validate_packet(packet)
        for offset in range(0, len(packet), 7):
            data = bytes((offset,)) + packet[offset:offset + 7]
            if self.write_guard is not None:
                self.write_guard()
            self._send(5, data)
        data = bytes((LOCAL, 0)) + struct.pack('>HH', len(packet), crc16(packet))
        if self.write_guard is not None:
            self.write_guard()
        self._send(7, data)


class Session:
    def __init__(self, transport):
        self.transport = transport
        self._identified = False

    def identify(self):
        self._identified = False
        self.transport.wait_live()
        identity = decode_firmware(self.transport.request(0))
        if not identity_matches(identity):
            raise ProtocolError('firmware / hardware / UUID identity mismatch')
        self._identified = True
        return identity

    def _require_identity(self):
        if not self._identified:
            raise ProtocolError('same-session identification required')

    def read_limits(self):
        self._require_identity()
        return decode_limits(self.transport.request(91))

    def apply(self, packet, braking_scale):
        """Send once, preserving whatever braking scale the ECU reports live."""
        self._require_identity()
        _validate_packet(packet)
        if not (isinstance(braking_scale, float) and math.isfinite(braking_scale)
                and 0 <= braking_scale <= 1):
            raise ProtocolError('braking scale out of range')
        safe = packet[:5] + struct.pack('>f', braking_scale) + packet[9:]
        self.transport.send_packet(_validate_packet(safe))
