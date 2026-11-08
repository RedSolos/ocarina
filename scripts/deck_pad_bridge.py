#!/usr/bin/env python3
"""Send a Linux gamepad (the Steam Deck's) to a Mac that runs a game with DM_PAD_UDP, so the game plays it as a controller with analog sticks.

Why: while the Mac is streamed to a Steam Deck through Steam Link, macOS gives Steam Input no way to make a virtual gamepad, so the Deck's controls reach the game as keyboard and mouse.
This bridge runs on the Deck, reads the gamepad Steam presents to the programs it launches (a Microsoft X-Box 360 pad, from /dev/input/event*) and sends its state in UDP datagrams
(dm/host/pad_net.h has the format); the Mac's NXNative plays them as one more controller. Python 3.9 or later, no packages.

On the Mac:  DM_PAD_UDP=9777 <the game's launch>        (see docs/env_switches.tsv; with the app, set it in the launcher or run the app's binary from a terminal)
On the Deck: python3 deck_pad_bridge.py <the Mac's name or address>
             python3 deck_pad_bridge.py --list           the gamepads it can see
             python3 deck_pad_bridge.py <mac> --print    also print the state, to see what a press does

In Game Mode Steam lets only the program it launched see the gamepad it makes, so start the bridge from the same shortcut as Steam Link: scripts/deck_launch_steamlink.sh.
The Deck's layout for that shortcut must make a gamepad (the "Gamepad" template). If /dev/input/event* is not readable, add the deck user to the input group.
"""
import argparse
import errno
import fcntl
import math
import os
import select
import signal
import socket
import struct
import sys
import time
from typing import Callable, Optional

# nn::hid::NpadButton bit order (dm/host/dm_input.h)
A, B, X, Y, LS, RS, L, R, ZL, ZR, PLUS, MINUS, LEFT, UP, RIGHT, DOWN = (1 << i for i in range(16))
STICK_MAX = 32767
MAGIC = b'NXPD'
VERSION = 1

# linux/input-event-codes.h
EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ, ABS_HAT0X, ABS_HAT0Y = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x10, 0x11
XBOX_A, XBOX_B, XBOX_X, XBOX_Y = 0x130, 0x131, 0x133, 0x134   # BTN_SOUTH, BTN_EAST, BTN_X (= BTN_NORTH), BTN_Y (= BTN_WEST): the kernel's codes for an Xbox pad's labelled buttons
BTN_TL, BTN_TR, BTN_TL2, BTN_TR2, BTN_SELECT, BTN_START, BTN_THUMBL, BTN_THUMBR = 0x136, 0x137, 0x138, 0x139, 0x13A, 0x13B, 0x13D, 0x13E
BTN_DPAD_UP, BTN_DPAD_DOWN, BTN_DPAD_LEFT, BTN_DPAD_RIGHT = 0x220, 0x221, 0x222, 0x223
KEY_MAX = 0x2FF
EVENT = struct.Struct('@llHHi')   # struct input_event: timeval, type, code, value (24 bytes on the Deck's 64-bit Linux)


def _ioc_read(nr: int, size: int) -> int:   # _IOR('E', nr, size)
    return (2 << 30) | (size << 16) | (ord('E') << 8) | nr


def face_map(nintendo: bool) -> dict[int, int]:
    """The face buttons by position, as the Switch has them (Xbox B on the right is the Switch's A); nintendo=True keeps the labels instead (Steam's "Nintendo layout")."""
    if nintendo:
        faces = {XBOX_A: A, XBOX_B: B, XBOX_X: X, XBOX_Y: Y}
    else:   # the Xbox's B is on the right, as the Switch's A; its A at the bottom is the Switch's B; its Y on top is the Switch's X; its X on the left is the Switch's Y
        faces = {XBOX_B: A, XBOX_A: B, XBOX_Y: X, XBOX_X: Y}
    return {**faces, BTN_TL: L, BTN_TR: R, BTN_TL2: ZL, BTN_TR2: ZR, BTN_START: PLUS, BTN_SELECT: MINUS, BTN_THUMBL: LS, BTN_THUMBR: RS,
            BTN_DPAD_UP: UP, BTN_DPAD_DOWN: DOWN, BTN_DPAD_LEFT: LEFT, BTN_DPAD_RIGHT: RIGHT}


class Config:
    def __init__(self, deadzone: float = 0.08, trigger: float = 0.5, nintendo: bool = False) -> None:
        self.deadzone = deadzone
        self.trigger = trigger
        self.buttons = face_map(nintendo)


class Pad:
    """The state of one gamepad, from its input events."""

    def __init__(self, cfg: Config, ranges: Optional[dict[int, tuple[int, int]]] = None) -> None:
        self.cfg = cfg
        self.keys: set[int] = set()
        self.abs: dict[int, int] = {}
        # what the kernel reports for an Xbox 360 pad; the real ranges replace them when the device tells (ioctl)
        self.ranges: dict[int, tuple[int, int]] = {ABS_X: (-32768, 32767), ABS_Y: (-32768, 32767), ABS_RX: (-32768, 32767), ABS_RY: (-32768, 32767),
                                                   ABS_Z: (0, 255), ABS_RZ: (0, 255), ABS_HAT0X: (-1, 1), ABS_HAT0Y: (-1, 1)}
        if ranges:
            self.ranges.update(ranges)

    def event(self, etype: int, code: int, value: int) -> None:
        if etype == EV_KEY:
            (self.keys.add if value else self.keys.discard)(code)   # value 2 is auto-repeat: still down
        elif etype == EV_ABS:
            self.abs[code] = value

    def norm(self, code: int) -> float:
        """An axis from -1 to 1 (a trigger from 0 to 1 is the upper half)."""
        lo, hi = self.ranges.get(code, (-32768, 32767))
        if hi <= lo:
            return 0.0
        rest = (lo + hi) // 2   # an axis never reported rests at its middle
        return max(-1.0, min(1.0, 2.0 * (self.abs.get(code, rest) - lo) / (hi - lo) - 1.0))

    def trigger(self, code: int) -> float:
        lo, hi = self.ranges.get(code, (0, 255))
        return 0.0 if hi <= lo else max(0.0, min(1.0, (self.abs.get(code, lo) - lo) / (hi - lo)))

    def stick(self, xcode: int, ycode: int) -> tuple[int, int]:
        """A stick as the game takes it: round dead zone, the rest rescaled to the full range, up positive (evdev has down positive)."""
        x, y = self.norm(xcode), -self.norm(ycode)
        m = math.hypot(x, y)
        dz = self.cfg.deadzone
        if m <= dz:
            return 0, 0
        k = min(1.0, (m - dz) / (1.0 - dz)) / m
        return round(x * k * STICK_MAX), round(y * k * STICK_MAX)

    def snapshot(self) -> tuple[int, int, int, int, int]:
        """(buttons, lx, ly, rx, ry)"""
        m = 0
        for code in self.keys:
            m |= self.cfg.buttons.get(code, 0)
        hx, hy = self.abs.get(ABS_HAT0X, 0), self.abs.get(ABS_HAT0Y, 0)
        m |= (LEFT if hx < 0 else RIGHT if hx > 0 else 0) | (UP if hy < 0 else DOWN if hy > 0 else 0)
        if self.trigger(ABS_Z) >= self.cfg.trigger:
            m |= ZL
        if self.trigger(ABS_RZ) >= self.cfg.trigger:
            m |= ZR
        lx, ly = self.stick(ABS_X, ABS_Y)
        rx, ry = self.stick(ABS_RX, ABS_RY)
        return m, lx, ly, rx, ry


def encode(seq: int, state: tuple[int, int, int, int, int]) -> bytes:
    """One datagram (dm/host/pad_net.h)."""
    m, lx, ly, rx, ry = state
    return MAGIC + struct.pack('<BBHI4h', VERSION, 0, seq & 0xFFFF, m & 0xFFFF, *(max(-STICK_MAX, min(STICK_MAX, v)) for v in (lx, ly, rx, ry)))


def _bits(words: str) -> set[int]:
    """The set bits of a /proc/bus/input/devices capability line: 64-bit hex words, the highest first."""
    out: set[int] = set()
    for i, w in enumerate(reversed(words.split())):
        v = int(w, 16)
        out |= {i * 64 + b for b in range(64) if v >> b & 1}
    return out


def list_pads(proc: str = '/proc/bus/input/devices') -> list[tuple[str, str, int]]:
    """The gamepads: (event device path, name, rank), the likeliest first. A gamepad has the A button and both sticks; Steam's virtual Xbox 360 pad ranks first."""
    try:
        with open(proc) as f:
            text = f.read()
    except OSError:
        return []
    found = []
    for block in text.strip().split('\n\n'):
        name, handlers, keys, absb = '', '', set(), set()
        for line in block.splitlines():
            if line.startswith('N: Name='):
                name = line.split('=', 1)[1].strip().strip('"')
            elif line.startswith('H: Handlers='):
                handlers = line.split('=', 1)[1]
            elif line.startswith('B: KEY='):
                keys = _bits(line.split('=', 1)[1])
            elif line.startswith('B: ABS='):
                absb = _bits(line.split('=', 1)[1])
        ev = next((h for h in handlers.split() if h.startswith('event')), None)
        if not ev or XBOX_A not in keys or not {ABS_X, ABS_Y, ABS_RX, ABS_RY} <= absb:
            continue
        low = name.lower()
        rank = 0 if 'x-box 360' in low or 'steam virtual' in low else 1 if 'xbox' in low or 'steam' in low else 2
        found.append(('/dev/input/' + ev, name, rank))
    return sorted(found, key=lambda f: f[2])


def read_ranges(fd: int) -> dict[int, tuple[int, int]]:
    """The device's own ranges for its axes (EVIOCGABS); {} where the descriptor is no input device."""
    out: dict[int, tuple[int, int]] = {}
    for code in (ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ, ABS_HAT0X, ABS_HAT0Y):
        buf = bytearray(24)   # struct input_absinfo: value, minimum, maximum, fuzz, flat, resolution
        try:
            fcntl.ioctl(fd, _ioc_read(0x40 + code, 24), buf, True)
        except OSError:
            continue
        _value, lo, hi, *_ = struct.unpack('6i', bytes(buf))
        if hi > lo:
            out[code] = (lo, hi)
    return out


def read_pressed(fd: int, pad: Pad) -> None:
    """The buttons and axes already down when the bridge starts (EVIOCGKEY, EVIOCGABS)."""
    buf = bytearray(KEY_MAX // 8 + 1)
    try:
        fcntl.ioctl(fd, _ioc_read(0x18, len(buf)), buf, True)
        for code in range(KEY_MAX + 1):
            if buf[code // 8] >> (code % 8) & 1:
                pad.keys.add(code)
    except OSError:
        pass
    for code in (ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ, ABS_HAT0X, ABS_HAT0Y):
        a = bytearray(24)
        try:
            fcntl.ioctl(fd, _ioc_read(0x40 + code, 24), a, True)
            pad.abs[code] = struct.unpack('6i', bytes(a))[0]
        except OSError:
            pass


def pump(fd: int, pad: Pad, send: Callable[[tuple[int, int, int, int, int]], None], stop: Callable[[], bool], keepalive: float = 0.1, show: bool = False) -> None:
    """Read the device until it goes away or stop() is true; send the state at every change and every `keepalive` seconds while nothing changes."""
    last = pad.snapshot()
    send(last)
    sent = time.monotonic()
    buf = b''
    while not stop():
        wait = max(0.0, sent + keepalive - time.monotonic())
        ready, _, _ = select.select([fd], [], [], wait)
        changed = False
        if ready:
            try:
                data = os.read(fd, EVENT.size * 64)
            except BlockingIOError:
                continue
            if not data:
                return   # the writer of a pipe closed (a test), or the device is gone
            buf += data
            while len(buf) >= EVENT.size:
                _s, _us, etype, code, value = EVENT.unpack_from(buf)
                buf = buf[EVENT.size:]
                pad.event(etype, code, value)
                if etype == EV_SYN and code == 0:
                    changed = True   # a report is complete: its state is whole
        now = time.monotonic()
        cur = pad.snapshot()
        if (changed and cur != last) or now - sent >= keepalive:
            if show and cur != last:
                print('buttons %04x  L(%6d,%6d)  R(%6d,%6d)' % cur, flush=True)
            send(cur)
            last, sent = cur, now


class Link:
    def __init__(self) -> None:
        self.seq = 0
        self.stop = False


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or '').split('\n\n')[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('host', nargs='?', help="the Mac's name or address")
    ap.add_argument('--port', type=int, default=9777, help='the UDP port of DM_PAD_UDP (default 9777)')
    ap.add_argument('--device', help='the gamepad, /dev/input/eventN (default: the likeliest one, as --list shows)')
    ap.add_argument('--list', action='store_true', help='list the gamepads and stop')
    ap.add_argument('--deadzone', type=float, default=0.08, help='the sticks round dead zone, 0 to 1 (default 0.08)')
    ap.add_argument('--trigger', type=float, default=0.5, help='how far an analog trigger goes to press ZL / ZR, 0 to 1 (default 0.5)')
    ap.add_argument('--nintendo-layout', action='store_true', help='keep the buttons labels (A is A) instead of their positions (the Xbox A at the bottom is the Switch B)')
    ap.add_argument('--grab', action='store_true', help='take the device for the bridge alone, so Steam Link no longer sends the same pad to the Mac as keyboard and mouse (it then also lacks the pad for its own menu)')
    ap.add_argument('--keepalive', type=float, default=0.1, help='seconds between datagrams while nothing changes (default 0.1)')
    ap.add_argument('--print', dest='show', action='store_true', help='print the state at every change')
    args = ap.parse_args(argv)

    if args.list:
        for path, name, _ in list_pads():
            print(f'{path}\t{name}')
        return 0
    if not args.host:
        ap.error('the Mac to send to is missing (or --list)')
    try:
        addr = socket.getaddrinfo(args.host, args.port, socket.AF_INET, socket.SOCK_DGRAM)[0][4]
    except socket.gaierror as e:
        print(f'cannot resolve {args.host}: {e}', file=sys.stderr)
        return 2
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cfg = Config(args.deadzone, args.trigger, args.nintendo_layout)
    state = Link()

    def send(st: tuple[int, int, int, int, int]) -> None:
        state.seq = (state.seq + 1) & 0xFFFF
        try:
            sock.sendto(encode(state.seq, st), addr)
        except OSError:
            pass   # the network is away for now: the next datagram tries again

    def quit_(*_a: object) -> None:
        state.stop = True
    signal.signal(signal.SIGINT, quit_)
    signal.signal(signal.SIGTERM, quit_)

    announced = ''
    while not state.stop:
        path = args.device
        if not path:
            pads = list_pads()
            path = pads[0][0] if pads else None
            if pads and announced != path:
                print(f'gamepad: {pads[0][1]} ({path})', file=sys.stderr)
        if not path:
            if announced != 'none':
                print('no gamepad yet: Steam makes it when the shortcut starts with a Gamepad layout; waiting', file=sys.stderr)
                announced = 'none'
            time.sleep(1.0)
            continue
        announced = path
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError as e:
            print(f'cannot open {path}: {e.strerror}' + (' (add the user to the input group)' if e.errno == errno.EACCES else ''), file=sys.stderr)
            time.sleep(1.0)
            continue
        try:
            if args.grab:
                fcntl.ioctl(fd, (1 << 30) | (4 << 16) | (ord('E') << 8) | 0x90, 1)   # EVIOCGRAB
            pad = Pad(cfg, read_ranges(fd))
            read_pressed(fd, pad)
            print(f'sending to {addr[0]}:{addr[1]}', file=sys.stderr)
            pump(fd, pad, send, lambda: state.stop, args.keepalive, args.show)
        except OSError as e:
            if e.errno != errno.ENODEV:
                print(f'{path}: {e.strerror}', file=sys.stderr)
        finally:
            os.close(fd)
        if not state.stop:
            print('the gamepad went away; looking for it again', file=sys.stderr)
            send((0, 0, 0, 0, 0))
            time.sleep(1.0)
    send((0, 0, 0, 0, 0))   # let go at once; the Mac would release after half a second of silence anyway
    return 0


if __name__ == '__main__':
    sys.exit(main())
