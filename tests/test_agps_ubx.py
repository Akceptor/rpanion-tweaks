import datetime
import struct
import unittest

from agps_loader import load_agps

agps = load_agps()


def reference_ck(body):
    a = b = 0
    for x in body:
        a = (a + x) & 0xFF
        b = (b + a) & 0xFF
    return bytes([a, b])


class UbxTest(unittest.TestCase):
    def test_mon_ver_poll(self):
        self.assertEqual(agps.mon_ver_poll().hex(), "b5620a0400000e34")

    def test_checksum_masks_8bit(self):
        f = agps.ubx_frame(0x13, 0x40, bytes([0xFF] * 40))
        self.assertEqual(f[-2:], reference_ck(f[2:-2]))

    def test_valset_use_ana(self):
        self.assertEqual(agps.valset_use_ana(), bytes.fromhex("b562068a0900000300000100231001d1d0"))

    def test_time_utc(self):
        now = datetime.datetime(2026, 10, 29, 12, 34, 56, 500000, tzinfo=datetime.timezone.utc)
        self.assertEqual(
            agps.mga_ini_time_utc(now).hex(),
            "b5621340180010000012ea070a1d0c2238000065cd1d01000000000000005be0",
        )

    def test_time_requires_utc(self):
        with self.assertRaises(ValueError):
            agps.mga_ini_time_utc(datetime.datetime(2026, 10, 29, 12, 0, 0))

    def test_pos_llh(self):
        self.assertEqual(
            agps.mga_ini_pos_llh(47.0, 8.0, 500.0, 25000.0).hex(),
            "b562134014000100000080a1031c00b4c40450c30000a025260022f1",
        )

    def test_pos_negative_and_rounding(self):
        f = agps.mga_ini_pos_llh(-12.07, -77.0435, 0.0, 50000.0)
        self.assertEqual(struct.unpack_from("<ii", f, 10), (-120700000, -770435000))

    def test_cfg_rst_warm(self):
        self.assertEqual(agps.cfg_rst(agps.RESET_MASKS["warm"]).hex(), "b5620604040001000200116c")

    def test_to_inject_pads_and_limits(self):
        n, data = agps.to_inject(bytes(range(1, 18)))
        self.assertEqual(n, 17)
        self.assertEqual(len(data), 110)
        self.assertEqual(data[:17], list(range(1, 18)))
        self.assertEqual(data[17:], [0] * 93)
        with self.assertRaises(ValueError):
            agps.to_inject(bytes(111))


if __name__ == "__main__":
    unittest.main()
