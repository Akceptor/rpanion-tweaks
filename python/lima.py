#!/usr/bin/env python3
import json
import math
import os
import tempfile

R_EARTH = 6378137.0


class Anchor:
    def __init__(self, real_lat, real_lon, fake_lat, fake_lon):
        self.real_lat = real_lat
        self.real_lon = real_lon
        self.fake_lat = fake_lat
        self.fake_lon = fake_lon

    def to_fake(self, lat, lon):
        d_n = (lat - self.real_lat) * R_EARTH * math.pi / 180
        d_e = (lon - self.real_lon) * R_EARTH * math.cos(math.radians(self.real_lat)) * math.pi / 180
        return (
            self.fake_lat + d_n / (R_EARTH * math.pi / 180),
            self.fake_lon + d_e / (R_EARTH * math.cos(math.radians(self.fake_lat)) * math.pi / 180),
        )

    def to_real(self, lat, lon):
        d_n = (lat - self.fake_lat) * R_EARTH * math.pi / 180
        d_e = (lon - self.fake_lon) * R_EARTH * math.cos(math.radians(self.fake_lat)) * math.pi / 180
        return (
            self.real_lat + d_n / (R_EARTH * math.pi / 180),
            self.real_lon + d_e / (R_EARTH * math.cos(math.radians(self.real_lat)) * math.pi / 180),
        )


def distance_m(lat1, lon1, lat2, lon2):
    mean_lat = math.radians((lat1 + lat2) / 2)
    d_n = math.radians(lat2 - lat1) * R_EARTH
    d_e = math.radians(lon2 - lon1) * R_EARTH * math.cos(mean_lat)
    return math.hypot(d_n, d_e)


class AnchorStore:
    def __init__(self, path):
        self.path = path

    def load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            return Anchor(
                float(d["real_lat"]), float(d["real_lon"]),
                float(d["fake_lat"]), float(d["fake_lon"]),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self, anchor):
        d = os.path.dirname(self.path) or "."
        fd, tmp = tempfile.mkstemp(dir=d)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({
                    "real_lat": anchor.real_lat, "real_lon": anchor.real_lon,
                    "fake_lat": anchor.fake_lat, "fake_lon": anchor.fake_lon,
                }, f)
            os.replace(tmp, self.path)
        except BaseException:
            os.unlink(tmp)
            raise
