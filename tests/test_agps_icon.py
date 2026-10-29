import unittest

from pymavlink.dialects.v20 import ardupilotmega as mavlink

from agps_loader import load_agps
from lima_frames import decode

agps = load_agps()

POS = agps.Position(47.0, 8.0, 400.0, 25000, "cache")


def gpi(lat=0, lon=0, alt=0, relative_alt=0, time_boot_ms=0):
    return mavlink.MAVLink_global_position_int_message(
        time_boot_ms=time_boot_ms, lat=lat, lon=lon, alt=alt, relative_alt=relative_alt,
        vx=5, vy=5, vz=5, hdg=100)


def one(frame):
    msgs = decode(frame)
    assert len(msgs) == 1
    return msgs[0]


class FrameTest(unittest.TestCase):
    def make(self):
        self.sent = []
        self.t = 0.0
        return agps.ApproxIcon(self.sent.append, clock=lambda: self.t)

    def test_frame_fields(self):
        icon = self.make()
        f = icon.frame(POS)
        self.assertEqual(f[0], 0xFD)
        m = one(f)
        self.assertEqual(m.get_msgId(), 33)
        self.assertEqual((m.get_srcSystem(), m.get_srcComponent()), (1, 1))
        self.assertEqual((m.lat, m.lon, m.alt, m.relative_alt, m.time_boot_ms), (470000000, 80000000, 400000, 0, 0))
        self.assertEqual((m.vx, m.vy, m.vz, m.hdg), (0, 0, 0, 65535))

    def test_copies_fc_altitude(self):
        icon = self.make()
        icon.fc_position(gpi(alt=123456, relative_alt=-50, time_boot_ms=99000))
        m = one(icon.frame(POS))
        self.assertEqual((m.alt, m.relative_alt, m.time_boot_ms), (123456, -50, 99000))
        self.assertEqual((m.vx, m.vy, m.vz, m.hdg), (0, 0, 0, 65535))

    def test_own_echo_ignored(self):
        icon = self.make()
        icon.fc_position(gpi(alt=10, relative_alt=20, time_boot_ms=30))
        icon.frame(POS)
        icon.fc_position(gpi(lat=470000000, lon=80000000, alt=1, relative_alt=1, time_boot_ms=1))
        m = one(icon.frame(POS))
        self.assertEqual((m.alt, m.relative_alt, m.time_boot_ms), (10, 20, 30))

    def test_seq_increments(self):
        icon = self.make()
        self.assertEqual([one(icon.frame(POS)).get_seq(), one(icon.frame(POS)).get_seq()], [0, 1])

    def test_never_gps_raw_int(self):
        icon = self.make()
        for i in range(50):
            self.t += 0.2
            icon.update(POS, True, True)
        self.assertEqual(len(self.sent), 50)
        self.assertEqual({one(f).get_type() for f in self.sent}, {"GLOBAL_POSITION_INT"})

    def test_rate(self):
        icon = self.make()
        icon.update(POS, True, True)
        self.assertEqual(len(self.sent), 1)
        self.t = 0.05
        icon.update(POS, True, True)
        self.assertEqual(len(self.sent), 1)
        self.t = 0.1
        icon.update(POS, True, True)
        self.assertEqual(len(self.sent), 2)
        self.t = 1.0
        icon.update(POS, True, False)
        self.assertEqual(len(self.sent), 2)
        self.t = 1.1
        icon.update(POS, True, False)
        self.assertEqual(len(self.sent), 3)
        self.t = 2.0
        icon.update(POS, True, False)
        self.assertEqual(len(self.sent), 3)
        self.t = 2.1
        icon.update(POS, True, False)
        self.assertEqual(len(self.sent), 4)

    def test_inactive_sends_nothing_and_rearms(self):
        icon = self.make()
        icon.update(POS, True, True)
        icon.update(POS, False, True)
        icon.update(None, True, True)
        self.assertEqual(len(self.sent), 1)
        self.t = 0.01
        icon.update(POS, False, True)
        icon.update(POS, True, True)
        self.assertEqual(len(self.sent), 2)

    def test_send_error_does_not_propagate(self):
        def boom(frame):
            raise BrokenPipeError()

        icon = agps.ApproxIcon(boom)
        icon.update(POS, True, True)


if __name__ == "__main__":
    unittest.main()
