"""scripts/deck_pad_bridge.py (the Steam Deck's gamepad sent to the Mac's DM_PAD_UDP, dm/host/pad_net.h): the datagram's bytes (the layout dm/host/tests/t_pad_net.c decodes), the button and
stick mapping, the gamepad search, and the event loop on a pipe in place of /dev/input/event*."""
import importlib.util
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
import unittest

REPO = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location('deck_pad_bridge', REPO / 'scripts/deck_pad_bridge.py')
assert spec and spec.loader
br = importlib.util.module_from_spec(spec)
spec.loader.exec_module(br)


def pad(**kw):
    return br.Pad(br.Config(**kw))


def press(p, *codes):
    for c in codes:
        p.event(br.EV_KEY, c, 1)


class Datagram(unittest.TestCase):
    def test_layout_matches_pad_net_h(self):
        b = br.encode(0xBEEF, (br.A | br.ZR | br.DOWN, 32767, -32767, 1234, -1))
        self.assertEqual(len(b), 20)
        self.assertEqual(b[:8], b'NXPD\x01\x00\xef\xbe')
        self.assertEqual(b[8:12], bytes([0x01, 0x82, 0, 0]))
        self.assertEqual(struct.unpack('<4h', b[12:]), (32767, -32767, 1234, -1))

    def test_sequence_wraps_and_axes_clamp(self):
        b = br.encode(0x10001, (0, 99999, -99999, 0, 0))
        self.assertEqual(struct.unpack('<H', b[6:8])[0], 1)
        self.assertEqual(struct.unpack('<2h', b[12:16]), (32767, -32767))


class Mapping(unittest.TestCase):
    def test_faces_by_position(self):
        # an Xbox pad: A bottom, B right, X left, Y top; the Switch has A right, B bottom, X top, Y left
        for code, want in ((br.XBOX_B, br.A), (br.XBOX_A, br.B), (br.XBOX_Y, br.X), (br.XBOX_X, br.Y)):
            p = pad(); press(p, code)
            self.assertEqual(p.snapshot()[0], want)

    def test_faces_by_label(self):
        for code, want in ((br.XBOX_A, br.A), (br.XBOX_B, br.B), (br.XBOX_X, br.X), (br.XBOX_Y, br.Y)):
            p = pad(nintendo=True); press(p, code)
            self.assertEqual(p.snapshot()[0], want)

    def test_other_buttons_and_release(self):
        p = pad(); press(p, br.BTN_TL, br.BTN_TR, br.BTN_START, br.BTN_SELECT, br.BTN_THUMBL, br.BTN_THUMBR)
        self.assertEqual(p.snapshot()[0], br.L | br.R | br.PLUS | br.MINUS | br.LS | br.RS)
        p.event(br.EV_KEY, br.BTN_TL, 0)
        p.event(br.EV_KEY, br.BTN_TR, 2)   # auto-repeat: still down
        self.assertEqual(p.snapshot()[0], br.R | br.PLUS | br.MINUS | br.LS | br.RS)

    def test_dpad_hat_and_buttons(self):
        p = pad(); p.event(br.EV_ABS, br.ABS_HAT0X, -1); p.event(br.EV_ABS, br.ABS_HAT0Y, 1)
        self.assertEqual(p.snapshot()[0], br.LEFT | br.DOWN)
        p.event(br.EV_ABS, br.ABS_HAT0X, 0); p.event(br.EV_ABS, br.ABS_HAT0Y, -1)
        self.assertEqual(p.snapshot()[0], br.UP)
        p = pad(); press(p, br.BTN_DPAD_RIGHT)
        self.assertEqual(p.snapshot()[0], br.RIGHT)

    def test_analog_triggers_press_zl_zr_past_the_threshold(self):
        p = pad()
        p.event(br.EV_ABS, br.ABS_Z, 100); p.event(br.EV_ABS, br.ABS_RZ, 200)   # 0..255: 39% and 78%
        self.assertEqual(p.snapshot()[0], br.ZR)
        p.event(br.EV_ABS, br.ABS_Z, 130)
        self.assertEqual(p.snapshot()[0], br.ZL | br.ZR)
        self.assertEqual(pad(trigger=0.2).snapshot()[0], 0)

    def test_sticks_up_is_positive_and_the_dead_zone_rests(self):
        p = pad()
        self.assertEqual(p.snapshot()[1:], (0, 0, 0, 0))                          # nothing reported: centred
        p.event(br.EV_ABS, br.ABS_X, 32767)                                       # left stick full right
        self.assertEqual(p.snapshot()[1:], (32767, 0, 0, 0))
        p.event(br.EV_ABS, br.ABS_X, 0); p.event(br.EV_ABS, br.ABS_Y, -32768)     # full up: evdev has up negative, the game up positive
        self.assertEqual(p.snapshot()[1:], (0, 32767, 0, 0))
        p.event(br.EV_ABS, br.ABS_Y, 0); p.event(br.EV_ABS, br.ABS_RX, -32768)    # right stick full left
        self.assertEqual(p.snapshot()[1:], (0, 0, -32767, 0))
        p.event(br.EV_ABS, br.ABS_RX, 0); p.event(br.EV_ABS, br.ABS_RY, 32767)    # right stick full down
        self.assertEqual(p.snapshot()[1:], (0, 0, 0, -32767))
        p.event(br.EV_ABS, br.ABS_RY, 0); p.event(br.EV_ABS, br.ABS_X, 1500); p.event(br.EV_ABS, br.ABS_Y, -1500)   # 3.2% each way: inside the 8% dead zone
        self.assertEqual(p.snapshot()[1:], (0, 0, 0, 0))
        p.event(br.EV_ABS, br.ABS_X, 20000); p.event(br.EV_ABS, br.ABS_Y, -20000)   # a diagonal keeps its direction
        _m, lx, ly, _rx, _ry = p.snapshot()
        self.assertAlmostEqual(lx, ly, delta=1); self.assertGreater(lx, 10000)

    def test_stick_past_the_dead_zone_starts_from_zero(self):
        p = pad(deadzone=0.5)
        p.event(br.EV_ABS, br.ABS_X, 16384 + 100)   # a hair past 50%
        self.assertLess(p.snapshot()[1], 400)
        p.event(br.EV_ABS, br.ABS_X, 32767)
        self.assertEqual(p.snapshot()[1], 32767)

    def test_device_ranges(self):
        p = br.Pad(br.Config(), {br.ABS_X: (0, 255), br.ABS_Y: (0, 255)})   # a pad with unsigned 8-bit sticks
        p.event(br.EV_ABS, br.ABS_X, 255)
        lx, ly = p.snapshot()[1:3]
        self.assertEqual(lx, 32767); self.assertLess(abs(ly), 300)                  # 127 of 0..255 is one count off the true middle: a hair, not the dead zone's job
        p.event(br.EV_ABS, br.ABS_X, 128); p.event(br.EV_ABS, br.ABS_Y, 255)      # y at its top is down in evdev: negative for the game
        lx, ly = p.snapshot()[1:3]
        self.assertEqual(ly, -32767); self.assertLess(abs(lx), 300)


PROC = '''I: Bus=0003 Vendor=28de Product=1205 Version=0111
N: Name="Steam Deck"
H: Handlers=kbd mouse0 event3
B: PROP=0
B: EV=1f
B: KEY=ffff000000000000 0 0 0 0

I: Bus=0003 Vendor=045e Product=028e Version=0110
N: Name="Microsoft X-Box 360 pad"
H: Handlers=event7 js0
B: PROP=0
B: EV=20000b
B: KEY=7cdb000000000000 0 0 0 0
B: ABS=3003f

I: Bus=0003 Vendor=1234 Product=5678 Version=0001
N: Name="Some Gamepad"
H: Handlers=event9 js1
B: KEY=7cdb000000000000 0 0 0 0
B: ABS=3003f

I: Bus=0011 Vendor=0001 Product=0001 Version=ab41
N: Name="AT Translated Set 2 keyboard"
H: Handlers=sysrq kbd event0
B: KEY=402000000 3803078f800d001 feffffdfffefffff fffffffffffffffe
'''


class Search(unittest.TestCase):
    def test_gamepads_by_capability_the_virtual_xbox_pad_first(self):
        with tempfile.NamedTemporaryFile('w', suffix='.txt') as f:
            f.write(PROC); f.flush()
            pads = br.list_pads(f.name)
        self.assertEqual([p[0] for p in pads], ['/dev/input/event7', '/dev/input/event9'])   # not the Deck's own keyboard-and-mouse device, not the keyboard
        self.assertEqual(pads[0][1], 'Microsoft X-Box 360 pad')

    def test_no_proc_file(self):
        self.assertEqual(br.list_pads('/nonexistent/devices'), [])


class Loop(unittest.TestCase):
    def test_events_on_a_pipe_become_datagrams(self):
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(('127.0.0.1', 0)); rx.settimeout(2.0)
        tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        seq = [0]

        def send(st):
            seq[0] += 1
            tx.sendto(br.encode(seq[0], st), rx.getsockname())
        r, w = os.pipe()
        p = pad()
        stop = threading.Event()
        t = threading.Thread(target=br.pump, args=(r, p, send, stop.is_set, 0.05))
        t.start()

        def ev(etype, code, value):
            return br.EVENT.pack(0, 0, etype, code, value)
        syn = ev(br.EV_SYN, 0, 0)

        def last_state():
            data = rx.recv(64)
            while True:   # the newest datagram that is waiting
                rx.settimeout(0.05)
                try:
                    data = rx.recv(64)
                except socket.timeout:
                    break
            rx.settimeout(2.0)
            return struct.unpack('<I4h', data[8:])
        self.assertEqual(last_state(), (0, 0, 0, 0, 0))                     # the first datagram: everything at rest
        os.write(w, ev(br.EV_KEY, br.XBOX_B, 1) + ev(br.EV_ABS, br.ABS_X, 32767) + ev(br.EV_ABS, br.ABS_RY, -32768) + syn)
        time.sleep(0.2)
        m, lx, ly, rx_, ry = last_state()
        self.assertEqual((m, lx, ly, rx_), (br.A, 32767, 0, 0)); self.assertEqual(ry, 32767)   # the right stick pushed up (evdev -32768) is positive
        time.sleep(0.2)
        self.assertEqual(last_state()[0], br.A)                             # nothing changed: the keepalive carries the held button
        os.write(w, ev(br.EV_KEY, br.XBOX_B, 0) + syn)
        time.sleep(0.2)
        self.assertEqual(last_state()[0], 0)
        stop.set(); os.close(w); t.join(2.0)
        self.assertFalse(t.is_alive())
        os.close(r); rx.close(); tx.close()


if __name__ == '__main__':
    unittest.main()
