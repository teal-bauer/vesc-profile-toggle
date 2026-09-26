# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 teal-bauer
"""Read-only probe: identify the ECU, then dump consecutive limit readbacks.

Establishes which fields move between reads while the vehicle sits still. It
sends only opcodes 0 and 91, so it never writes a profile.
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

import vesc_profile_io as io
import vesc_profile_toggle as togg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--interface', default='can0')
    parser.add_argument('--reads', type=int, default=12)
    parser.add_argument('--interval', type=float, default=0.25)
    parser.add_argument('--timeout', type=float, default=25.0)
    args = parser.parse_args()

    deadline = time.monotonic() + args.timeout
    while True:
        try:
            with io.SocketTransport(args.interface) as transport:
                identity = io.Session(transport).identify()
            break
        except Exception as error:
            if time.monotonic() > deadline:
                print('identify failed:', error)
                return 1
            time.sleep(0.5)
    print('identity', identity)

    reads = []
    with io.SocketTransport(args.interface) as transport:
        session = io.Session(transport)
        session.identify()
        for index in range(args.reads):
            limits = session.read_limits()
            reads.append(limits)
            print(index, json.dumps({key: limits[key] for key in togg.REPORTED_KEYS}, sort_keys=True))
            time.sleep(args.interval)

    first = reads[0]
    for index, limits in enumerate(reads):
        print('diff', index, togg.differing(first, limits, togg.REPORTED_KEYS),
              'class', io.classify_limits(limits))
    return 0


if __name__ == '__main__':
    sys.exit(main())
