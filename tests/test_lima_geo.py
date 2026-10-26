import math
import os
import stat
import tempfile
import unittest

from lima_loader import load_lima

lima = load_lima()

REAL = (47.0, 8.0)
FAKE = (-12.07, -77.0435)


def anchor():
    return lima.Anchor(*REAL, *FAKE)


class GeoTest(unittest.TestCase):
    def test_anchor_maps_to_fake_anchor(self):
        lat, lon = anchor().to_fake(*REAL)
        self.assertAlmostEqual(lat, FAKE[0], delta=1e-9)
        self.assertAlmostEqual(lon, FAKE[1], delta=1e-9)

    def test_offset_preserves_distance_and_bearing(self):
        r = lima.R_EARTH
        lat = REAL[0] + math.degrees(100 / r)
        lon = REAL[1] + math.degrees(100 / (r * math.cos(math.radians(REAL[0]))))
        flat, flon = anchor().to_fake(lat, lon)
        self.assertAlmostEqual(lima.distance_m(*FAKE, flat, flon), 141.42, delta=0.1)
        north = lima.distance_m(*FAKE, flat, FAKE[1])
        east = lima.distance_m(*FAKE, FAKE[0], flon)
        self.assertAlmostEqual(north, 100, delta=0.1)
        self.assertAlmostEqual(east, 100, delta=0.1)

    def test_round_trip_int_e7(self):
        a = anchor()
        r = lima.R_EARTH
        for dn in (-5000, -1234, 0, 777, 5000):
            for de in (-5000, -42, 0, 3000, 5000):
                lat = REAL[0] + math.degrees(dn / r)
                lon = REAL[1] + math.degrees(de / (r * math.cos(math.radians(REAL[0]))))
                lat7, lon7 = round(lat * 1e7), round(lon * 1e7)
                flat, flon = a.to_fake(lat7 / 1e7, lon7 / 1e7)
                flat7, flon7 = round(flat * 1e7), round(flon * 1e7)
                rlat, rlon = a.to_real(flat7 / 1e7, flon7 / 1e7)
                self.assertLessEqual(abs(round(rlat * 1e7) - lat7), 1)
                self.assertLessEqual(abs(round(rlon * 1e7) - lon7), 1)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "anchor.json")

    def test_store_round_trip_and_mode(self):
        store = lima.AnchorStore(self.path)
        store.save(anchor())
        loaded = store.load()
        a = anchor()
        self.assertEqual(
            (loaded.real_lat, loaded.real_lon, loaded.fake_lat, loaded.fake_lon),
            (a.real_lat, a.real_lon, a.fake_lat, a.fake_lon),
        )
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_store_bad_file(self):
        store = lima.AnchorStore(self.path)
        self.assertIsNone(store.load())
        open(self.path, "w").close()
        self.assertIsNone(store.load())
        with open(self.path, "w") as f:
            f.write("{")
        self.assertIsNone(store.load())


if __name__ == "__main__":
    unittest.main()
