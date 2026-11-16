"""Survey logging: one positioned CSV per session plus raw instrument/GPS captures."""
from __future__ import annotations

import csv
import datetime as dt
import os
import time

COLUMNS = ["pc_time", "t_mono", "line", "cond_mS_m", "inphase_ppt", "range", "dipole", "depth_m",
           "marker", "raw1", "raw2", "info_byte", "east", "north", "utm_zone",
           "ant_lat", "ant_lon", "alt_m", "fix_quality", "nsat", "hdop", "heading_deg",
           "gps_gap_s", "comment"]


class SurveyLogger:
    def __init__(self, folder: str, name: str | None = None):
        os.makedirs(folder, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.base = os.path.join(folder, name or f"EM31_{stamp}")
        self.csv_path = self.base + ".csv"
        self._f = open(self.csv_path, "w", newline="", buffering=1)
        self._w = csv.writer(self._f)
        self._w.writerow(COLUMNS)
        self._em_raw = open(self.base + "_em31_raw.bin", "wb")
        self._nmea = open(self.base + "_gps.nmea", "w", buffering=1)
        self.count = 0
        # map monotonic clock to wall clock once, so every row gets a real timestamp
        self._wall0 = time.time() - time.monotonic()
        self._last_flush = time.monotonic()

    def wall(self, t_mono: float) -> str:
        return dt.datetime.fromtimestamp(self._wall0 + t_mono).isoformat(timespec="milliseconds")

    def raw_em(self, data: bytes):
        self._em_raw.write(data)

    def raw_nmea(self, t: float, line: str):
        self._nmea.write(f"{self.wall(t)}\t{line}\n")

    def write(self, pr, line: str, zone, comment: str = "", depth: float | None = None):
        em, fx = pr.em, pr.fix
        f = lambda v, p=4: "" if v is None else f"{v:.{p}f}"
        self._w.writerow([
            self.wall(em.t), f"{em.t:.3f}", line, f(em.cond, 3), f(em.inphase, 4),
            em.range or "", "VMD" if em.vertical else "HMD", f(depth, 2), int(em.marker),
            em.raw1, em.raw2, em.info,
            f(pr.east, 3), f(pr.north, 3), zone or "",
            f(pr.lat, 8), f(pr.lon, 8),
            f(fx.alt if fx else None, 2), fx.quality if fx else "", fx.nsat if fx else "",
            f(fx.hdop if fx else None, 1), f(pr.heading, 1), f(pr.gps_gap, 2), comment])
        self.count += 1
        now = time.monotonic()
        if now - self._last_flush > 2.0:       # survive a crash or dead battery
            self._f.flush(); self._em_raw.flush()
            os.fsync(self._f.fileno())
            self._last_flush = now

    def comment(self, t: float, line: str, text: str):
        self._w.writerow([self.wall(t), f"{t:.3f}", line] + [""] * (len(COLUMNS) - 4) + [text])

    def close(self):
        for fh in (self._f, self._em_raw, self._nmea):
            try:
                fh.flush(); fh.close()
            except Exception:
                pass
