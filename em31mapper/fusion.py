"""Merge EM31 readings with GPS fixes by PC arrival time.

EM readings are held until a GPS fix that is newer than them arrives, then
positioned by linear interpolation between the bracketing fixes. A configurable
layback shifts the position from the GPS antenna back along the direction of
travel to the EM31 boom centre (negative = sensor ahead of antenna), and a
cross-track offset moves it to the right (+) or left (-) of travel.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

from .em31 import EM31Reading
from .geo import to_utm, utm_zone
from .nmea import GPSFix


@dataclass
class PositionedReading:
    em: EM31Reading
    fix: GPSFix | None            # nearest fix used (for quality fields)
    lat: float | None
    lon: float | None
    east: float | None
    north: float | None
    heading: float | None         # degrees, clockwise from grid north
    gps_gap: float | None         # time between bracketing fixes (s)


class Fuser:
    def __init__(self):
        self.gps_latency = 0.0        # s, subtracted from GPS arrival times
        self.em_latency = 0.0         # s, subtracted from EM arrival times
        self.layback = 0.0            # m, sensor behind antenna (+)
        self.cross_offset = 0.0       # m, sensor right of antenna (+)
        self.max_gap = 3.0            # s, larger GPS gaps -> unpositioned
        self.zone: int | None = None
        self.north_hemi = True
        self._fixes: deque[tuple[float, float, float, GPSFix]] = deque(maxlen=400)
        self._pending: deque[EM31Reading] = deque()
        self._heading: float | None = None

    def reset(self):
        self._fixes.clear()
        self._pending.clear()
        self._heading = None

    # -- inputs -------------------------------------------------------------
    def add_em(self, r: EM31Reading) -> list[PositionedReading]:
        r.t -= self.em_latency
        self._pending.append(r)
        return self._flush_stale(r.t)

    def add_fix(self, fix: GPSFix) -> list[PositionedReading]:
        fix.t -= self.gps_latency
        if self.zone is None:
            self.zone = utm_zone(fix.lat, fix.lon)
            self.north_hemi = fix.lat >= 0
        e, n = to_utm(fix.lat, fix.lon, self.zone, self.north_hemi)
        if self._fixes:
            pe, pn = self._fixes[-1][1], self._fixes[-1][2]
            if math.hypot(e - pe, n - pn) > 0.3:      # update heading only when moving
                self._heading = math.degrees(math.atan2(e - pe, n - pn)) % 360
        self._fixes.append((fix.t, e, n, fix))
        return self._resolve()

    def flush_all(self) -> list[PositionedReading]:
        out = self._resolve()
        while self._pending:
            out.append(self._unpositioned(self._pending.popleft()))
        return out

    # -- internals ----------------------------------------------------------
    def _flush_stale(self, now: float) -> list[PositionedReading]:
        out = []
        while self._pending and now - self._pending[0].t > self.max_gap + 1.0:
            out.append(self._unpositioned(self._pending.popleft()))
        return out

    def _unpositioned(self, r):
        return PositionedReading(r, None, None, None, None, None, None, None)

    def _resolve(self) -> list[PositionedReading]:
        out = []
        if not self._fixes:
            return out
        last_t = self._fixes[-1][0]
        while self._pending and self._pending[0].t <= last_t:
            out.append(self._interp(self._pending.popleft()))
        return out

    def _interp(self, r: EM31Reading) -> PositionedReading:
        fx = self._fixes
        # search backwards for bracketing pair
        i = len(fx) - 1
        while i > 0 and fx[i - 1][0] > r.t:
            i -= 1
        if i == 0:
            if fx[0][0] - r.t > 0.5:          # before first fix
                return self._unpositioned(r)
            t1, e1, n1, f1 = fx[0]
            e, n, gap, f = e1, n1, 0.0, f1
            hdg = self._heading
        else:
            t0, e0, n0, f0 = fx[i - 1]
            t1, e1, n1, f1 = fx[i]
            gap = t1 - t0
            if gap > self.max_gap:
                return self._unpositioned(r)
            w = 0.0 if gap <= 0 else (r.t - t0) / gap
            e = e0 + w * (e1 - e0)
            n = n0 + w * (n1 - n0)
            f = f1 if w >= 0.5 else f0
            hdg = (math.degrees(math.atan2(e1 - e0, n1 - n0)) % 360
                   if math.hypot(e1 - e0, n1 - n0) > 0.05 else self._heading)
        if hdg is not None and (self.layback or self.cross_offset):
            h = math.radians(hdg)
            ue, un = math.sin(h), math.cos(h)          # unit along travel
            re_, rn = un, -ue                          # unit to the right
            e += -self.layback * ue + self.cross_offset * re_
            n += -self.layback * un + self.cross_offset * rn
        lat, lon = self._lat_lon_interp(r.t)
        return PositionedReading(r, f, lat, lon, e, n, hdg, gap)

    def _lat_lon_interp(self, t):
        fx = self._fixes
        i = len(fx) - 1
        while i > 0 and fx[i - 1][0] > t:
            i -= 1
        if i == 0:
            return fx[0][3].lat, fx[0][3].lon
        t0, f0 = fx[i - 1][0], fx[i - 1][3]
        t1, f1 = fx[i][0], fx[i][3]
        w = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
        return f0.lat + w * (f1.lat - f0.lat), f0.lon + w * (f1.lon - f0.lon)

    def sensor_position(self, e: float, n: float) -> tuple[float, float]:
        """Antenna E/N -> sensor E/N using the current heading and offsets."""
        if self._heading is None or not (self.layback or self.cross_offset):
            return e, n
        h = math.radians(self._heading)
        ue, un = math.sin(h), math.cos(h)
        return (e - self.layback * ue + self.cross_offset * un,
                n - self.layback * un - self.cross_offset * ue)
