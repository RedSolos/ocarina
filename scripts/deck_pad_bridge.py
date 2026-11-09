#!/usr/bin/env python3
"""Send a Linux gamepad (the Steam Deck's) to a Mac that runs a game with DM_PAD_UDP, so the game plays it as a controller with analog sticks.

Why: while the Mac is streamed to a Steam Deck through Steam Link, macOS gives Steam Input no way to make a virtual gamepad, so the Deck's controls reach the game as keyboard and mouse.
This bridge runs on the Deck, reads its controls and sends their state in UDP datagrams (dm/host/pad_net.h has the format); the Mac's NXNative plays them as one more controller.
Python 3.9 or later, no packages. Two sources:
  deck   the Deck's own controller, read from its hidraw device (Valve 28DE:1205, the reports SDL's SDL_hidapi_steamdeck.c reads) beside Steam, which keeps it. It works whatever
         streams the Mac (Steam's Remote Play from the library, or the Steam Link app) and needs no shortcut of its own, so the bridge can run as a service (deck_pad_service.sh)
  evdev  a gamepad from /dev/input/event*: in Game Mode Steam lets only the program it launched see the gamepad it makes (an Xbox 360 pad), so the bridge starts from the same
         shortcut as Steam Link (deck_launch_steamlink.sh, whose layout must be the "Gamepad" template); on another Linux machine, any gamepad
auto (the default) takes the Deck's controller when there is one.

On the Mac:  DM_PAD_UDP=9777 <the game's launch>        (see docs/env_switches.tsv; NX Play and the Mac app's launcher show it as "Network controller")
On the Deck: python3 deck_pad_bridge.py <the Mac's name or address>
             python3 deck_pad_bridge.py --list                     what it can read
             python3 deck_pad_bridge.py <mac> --print              also print the state, to see what a press does
             python3 deck_pad_bridge.py <mac> --when streaming     send only while the Deck streams (Remote Play or Steam Link), as the service does

If /dev/input/event* is not readable, add the deck user to the input group; the Deck's hidraw device is the logged-in user's already (Steam's udev rules).
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


# the Steam Deck's controller (SDL's SDL_hidapi_steamdeck.c and steam/controller_structs.h): 64-byte input reports, little endian, no report ID
DECK_VID, DECK_PID = 0x28DE, 0x1205
DECK_REPORT_SIZE, DECK_STATE = 64, 9   # ValveInReportHeader_t: version 1, type ID_CONTROLLER_DECK_STATE, length 64
# header; packet number; buttons low, high; left pad x y, right pad x y; accel x y z; gyro x y z; quaternion w x y z; triggers L R (0..32767); left stick x y, right stick x y (up positive);
# pad pressures L R
DECK = struct.Struct('<HBBIII4h3h3h4hHH4hHH')
D_R2, D_L2, D_R, D_L, D_Y, D_B, D_X, D_A = 0x1, 0x2, 0x4, 0x8, 0x10, 0x20, 0x40, 0x80
D_UP, D_RIGHT, D_LEFT, D_DOWN, D_VIEW, D_STEAM, D_MENU = 0x100, 0x200, 0x400, 0x800, 0x1000, 0x2000, 0x4000
D_L3, D_R3 = 0x400000, 0x4000000
# processes that show the Deck streaming another machine: Steam's Remote Play client, and the Steam Link app (flatpak)
STREAMERS = ('streaming_client', 'SteamLink', 'steamlink')


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


def shape_stick(x: float, y: float, deadzone: float) -> tuple[int, int]:
    """A stick from -1..1 (up positive) as the game takes it: round dead zone, the rest rescaled to the full range."""
    m = math.hypot(x, y)
    if m <= deadzone:
        return 0, 0
    k = min(1.0, (m - deadzone) / (1.0 - deadzone)) / m
    return round(max(-1.0, min(1.0, x * k)) * STICK_MAX), round(max(-1.0, min(1.0, y * k)) * STICK_MAX)


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
        self.buf = b''
        self.read_size = EVENT.size * 64

    def feed(self, data: bytes) -> bool:
        """Bytes read from the device; True when a report completed (its state is whole)."""
        self.buf += data
        done = False
        while len(self.buf) >= EVENT.size:
            _s, _us, etype, code, value = EVENT.unpack_from(self.buf)
            self.buf = self.buf[EVENT.size:]
            self.event(etype, code, value)
            done = done or (etype == EV_SYN and code == 0)
        return done

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
        return shape_stick(self.norm(xcode), -self.norm(ycode), self.cfg.deadzone)

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


class DeckPad:
    """The Steam Deck's own controller, from its hidraw reports. Read only: Steam keeps the device (its lizard mode, its watchdog) and reads the same reports."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.read_size = DECK_REPORT_SIZE
        self.state: tuple[int, int, int, int, int] = (0, 0, 0, 0, 0)
        if cfg.buttons[XBOX_A] == A:   # by label: the Deck's A is the Switch's A
            self.faces = {D_A: A, D_B: B, D_X: X, D_Y: Y}
        else:   # by position, as the Switch: the right button (Deck B) is A, the bottom one (Deck A) is B, the top one (Deck Y) is X, the left one (Deck X) is Y
            self.faces = {D_B: A, D_A: B, D_Y: X, D_X: Y}
        self.others = {D_L: L, D_R: R, D_MENU: PLUS, D_VIEW: MINUS, D_L3: LS, D_R3: RS, D_UP: UP, D_DOWN: DOWN, D_LEFT: LEFT, D_RIGHT: RIGHT}

    @staticmethod
    def is_state(data: bytes) -> bool:
        if len(data) < DECK.size:
            return False
        version, rtype, length = struct.unpack_from('<HBB', data)
        return version == 1 and rtype == DECK_STATE and length == DECK_REPORT_SIZE

    def feed(self, data: bytes) -> bool:
        """One report (hidraw gives one per read); True when it was the controller's state. The Steam and Quick Access buttons, the back grips and the pads stay Steam's."""
        if not self.is_state(data):
            return False
        f = DECK.unpack_from(data)
        low, tl, tr, lsx, lsy, rsx, rsy = f[4], f[20], f[21], f[22], f[23], f[24], f[25]
        m = 0
        for bit, btn in {**self.faces, **self.others}.items():
            if low & bit:
                m |= btn
        if tl / 32767 >= self.cfg.trigger:
            m |= ZL
        if tr / 32767 >= self.cfg.trigger:
            m |= ZR
        lx, ly = shape_stick(lsx / 32767, lsy / 32767, self.cfg.deadzone)
        rx, ry = shape_stick(rsx / 32767, rsy / 32767, self.cfg.deadzone)
        self.state = (m, lx, ly, rx, ry)
        return True

    def snapshot(self) -> tuple[int, int, int, int, int]:
        return self.state


def deck_hidraws(sysfs: str = '/sys/class/hidraw') -> list[str]:
    """The hidraw devices of the Deck's controller (Valve 28DE:1205). It has several (its keyboard and mouse too): only the controller's sends state reports (probe_deck)."""
    out = []
    try:
        names = sorted(os.listdir(sysfs))
    except OSError:
        return []
    want = 'HID_ID=0003:%08X:%08X' % (DECK_VID, DECK_PID)
    for name in names:
        try:
            with open(os.path.join(sysfs, name, 'device', 'uevent')) as f:
                if want in f.read().upper():
                    out.append('/dev/' + name)
        except OSError:
            continue
    return out


def probe_deck(paths: list[str], wait: float = 0.2) -> Optional[str]:
    """The one of `paths` that sends the controller's state reports (about every 4 ms)."""
    for path in paths:
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            end = time.monotonic() + wait
            while time.monotonic() < end:
                ready, _, _ = select.select([fd], [], [], max(0.0, end - time.monotonic()))
                if not ready:
                    break
                try:
                    if DeckPad.is_state(os.read(fd, DECK_REPORT_SIZE)):
                        return path
                except BlockingIOError:
                    continue
        finally:
            os.close(fd)
    return None


def streaming(proc: str = '/proc') -> bool:
    """The Deck streams another machine now: Steam's Remote Play client or the Steam Link app runs (the base name of a process's program)."""
    try:
        pids = [p for p in os.listdir(proc) if p.isdigit()]
    except OSError:
        return False
    for pid in pids:
        try:
            with open(os.path.join(proc, pid, 'cmdline'), 'rb') as f:
                argv0 = f.read().split(b'\0', 1)[0].decode(errors='replace')
        except OSError:
            continue
        if os.path.basename(argv0) in STREAMERS:
            return True
    return False


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


def pump(fd: int, pad, send: Callable[[tuple[int, int, int, int, int]], None], stop: Callable[[], bool], keepalive: float = 0.1, show: bool = False) -> None:
    """Read the device (a Pad's evdev events, or a DeckPad's hidraw reports) until it goes away or stop() is true; send the state at every change and every `keepalive` seconds
    while nothing changes."""
    last = pad.snapshot()
    send(last)
    sent = time.monotonic()
    while not stop():
        wait = max(0.0, sent + keepalive - time.monotonic())
        ready, _, _ = select.select([fd], [], [], wait)
        changed = False
        if ready:
            while ready:   # everything queued (the Deck reports every 4 ms), each read only once select says it will not block
                try:
                    data = os.read(fd, pad.read_size)
                except BlockingIOError:
                    break
                if not data:
                    return   # the writer of a pipe closed (a test), or the device is gone
                changed = pad.feed(data) or changed
                ready, _, _ = select.select([fd], [], [], 0)
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
    ap.add_argument('--source', choices=('auto', 'deck', 'evdev'), default='auto', help="what to read: the Deck's own controller (hidraw), a gamepad (/dev/input), or the Deck's when there is one (default)")
    ap.add_argument('--when', choices=('always', 'streaming'), default='always', help="send always (default), or only while the Deck streams (Steam's Remote Play or the Steam Link app)")
    ap.add_argument('--device', help='the device: /dev/hidrawN for deck, /dev/input/eventN for evdev (default: found, as --list shows)')
    ap.add_argument('--list', action='store_true', help='list the gamepads and stop')
    ap.add_argument('--deadzone', type=float, default=0.08, help='the sticks round dead zone, 0 to 1 (default 0.08)')
    ap.add_argument('--trigger', type=float, default=0.5, help='how far an analog trigger goes to press ZL / ZR, 0 to 1 (default 0.5)')
    ap.add_argument('--nintendo-layout', action='store_true', help='keep the buttons labels (A is A) instead of their positions (the Xbox A at the bottom is the Switch B)')
    ap.add_argument('--grab', action='store_true', help='evdev: take the device for the bridge alone, so Steam Link no longer sends the same pad to the Mac as keyboard and mouse (it then also lacks the pad for its own menu)')
    ap.add_argument('--keepalive', type=float, default=0.1, help='seconds between datagrams while nothing changes (default 0.1)')
    ap.add_argument('--print', dest='show', action='store_true', help='print the state at every change')
    args = ap.parse_args(argv)

    if args.list:
        deck = probe_deck(deck_hidraws())
        if deck:
            print(f'{deck}\tSteam Deck controller (--source deck)')
        for path, name, _ in list_pads():
            print(f'{path}\t{name} (--source evdev)')
        return 0
    if not args.host:
        ap.error('the Mac to send to is missing (or --list)')
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cfg = Config(args.deadzone, args.trigger, args.nintendo_layout)
    state = Link()
    net = {'addr': None, 'retry': 0.0, 'said': '', 'live': False, 'checked': 0.0, 'streaming': False}

    def address():   # resolved again after a failure: a service starts before the network, and a .local name can come and go
        if net['addr'] is None and time.monotonic() >= net['retry']:
            try:
                net['addr'] = socket.getaddrinfo(args.host, args.port, socket.AF_INET, socket.SOCK_DGRAM)[0][4]
                print(f'sending to {net["addr"][0]}:{net["addr"][1]}', file=sys.stderr)
                net['said'] = ''
            except (socket.gaierror, UnicodeError) as e:
                if net['said'] != str(e):
                    print(f'cannot resolve {args.host} ({e}); trying again every 5 s', file=sys.stderr)
                    net['said'] = str(e)
                net['retry'] = time.monotonic() + 5.0
        return net['addr']

    def active() -> bool:   # --when streaming: checked once a second, not at every report
        if args.when == 'always':
            return True
        now = time.monotonic()
        if now - net['checked'] >= 1.0:
            was, net['streaming'], net['checked'] = net['streaming'], streaming(), now
            if net['streaming'] != was:
                print('streaming: sending' if net['streaming'] else 'not streaming: quiet', file=sys.stderr)
        return net['streaming']

    def transmit(st: tuple[int, int, int, int, int]) -> None:
        addr = address()
        if addr is None:
            return
        state.seq = (state.seq + 1) & 0xFFFF
        try:
            sock.sendto(encode(state.seq, st), addr)
        except OSError:
            net['addr'] = None   # the network is away for now, or the address changed: resolve again
            net['retry'] = time.monotonic() + 1.0

    def send(st: tuple[int, int, int, int, int]) -> None:
        if active():
            net['live'] = True
            transmit(st)
        elif net['live']:   # streaming ended: let go at once (the Mac would after half a second), then stay quiet so the Mac's own players keep their slots
            net['live'] = False
            transmit((0, 0, 0, 0, 0))

    def quit_(*_a: object) -> None:
        state.stop = True
    signal.signal(signal.SIGINT, quit_)
    signal.signal(signal.SIGTERM, quit_)

    announced = ''
    while not state.stop:
        path, kind = args.device, None
        if path:
            kind = 'deck' if os.path.basename(path).startswith('hidraw') else 'evdev'
        else:
            if args.source in ('auto', 'deck'):
                path = probe_deck(deck_hidraws())
                kind = 'deck' if path else None
                if path and announced != path:
                    print(f'Steam Deck controller ({path})', file=sys.stderr)
            if not path and args.source in ('auto', 'evdev'):
                pads = list_pads()
                path = pads[0][0] if pads else None
                kind = 'evdev' if path else None
                if pads and announced != path:
                    print(f'gamepad: {pads[0][1]} ({path})', file=sys.stderr)
        if not path:
            if announced != 'none':
                print({'deck': "no Steam Deck controller found (is this a Deck?); waiting",
                       'evdev': 'no gamepad yet: Steam makes it when the shortcut starts with a Gamepad layout; waiting'}.get(args.source, 'no controller yet; waiting'), file=sys.stderr)
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
            if kind == 'deck':
                pad = DeckPad(cfg)
            else:
                if args.grab:
                    fcntl.ioctl(fd, (1 << 30) | (4 << 16) | (ord('E') << 8) | 0x90, 1)   # EVIOCGRAB
                pad = Pad(cfg, read_ranges(fd))
                read_pressed(fd, pad)
            pump(fd, pad, send, lambda: state.stop, args.keepalive, args.show)
        except OSError as e:
            if e.errno != errno.ENODEV:
                print(f'{path}: {e.strerror}', file=sys.stderr)
        finally:
            os.close(fd)
        if not state.stop:
            print('the controller went away; looking for it again', file=sys.stderr)
            send((0, 0, 0, 0, 0))
            time.sleep(1.0)
    if net['live'] or args.when == 'always':
        transmit((0, 0, 0, 0, 0))   # let go at once; the Mac would release after half a second of silence anyway
    return 0


if __name__ == '__main__':
    sys.exit(main())
