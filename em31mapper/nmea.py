"""Minimal NMEA-0183 reader for GGA / RMC / GSA from any talker (GP, GN, GL, ...)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GPSFix:
    t: float               # PC monotonic receive time (s), latency-corrected later
    utc: str               # hhmmss.ss
    lat: float
    lon: float
    alt: float | None
    quality: int           # 0 none, 1 GPS, 2 DGPS, 4 RTK fixed, 5 RTK float ...
    nsat: int
    hdop: float | None
    raw: str


QUALITY_NAMES = {0: "No fix", 1: "GPS", 2: "DGPS", 3: "PPS", 4: "RTK fix",
                 5: "RTK float", 6: "Dead reck.", 7: "Manual", 8: "Sim", 9: "SBAS"}


def checksum_ok(sentence: str) -> bool:
    if "*" not in sentence:
        return True  # some receivers omit it; accept
    body, _, cs = sentence[1:].partition("*")
    calc = 0
    for ch in body:
        calc ^= ord(ch)
    try:
        return calc == int(cs[:2], 16)
    except ValueError:
        return False


def _dm_to_deg(v: str, hemi: str) -> float:
    if not v:
        raise ValueError("empty coordinate")
    dot = v.index(".") if "." in v else len(v)
    deg = float(v[: dot - 2])
    minutes = float(v[dot - 2:])
    d = deg + minutes / 60.0
    return -d if hemi in ("S", "W") else d


class NMEAReader:
    def __init__(self):
        self._buf = ""
        self.pdop: float | None = None
        self.speed_mps: float | None = None
        self.course_deg: float | None = None
        self.bad_sentences = 0

    def feed(self, data: bytes, t: float) -> tuple[list[GPSFix], list[str]]:
        """Return (new fixes, all complete sentences seen)."""
        self._buf += data.decode("ascii", errors="replace")
        fixes, lines = [], []
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.strip()
            if not line.startswith("$"):
                continue
            lines.append(line)
            fix = self.parse(line, t)
            if fix is not None:
                fixes.append(fix)
        if len(self._buf) > 4096:
            self._buf = self._buf[-512:]
        return fixes, lines

    def parse(self, line: str, t: float) -> GPSFix | None:
        if not checksum_ok(line):
            self.bad_sentences += 1
            return None
        f = line.split("*")[0].split(",")
        kind = f[0][3:]
        try:
            if kind == "GGA" and len(f) >= 10:
                q = int(f[6] or 0)
                if q == 0 or not f[2]:
                    return None
                return GPSFix(
                    t=t, utc=f[1],
                    lat=_dm_to_deg(f[2], f[3]), lon=_dm_to_deg(f[4], f[5]),
                    alt=float(f[9]) if f[9] else None, quality=q,
                    nsat=int(f[7] or 0), hdop=float(f[8]) if f[8] else None,
                    raw=line)
            if kind == "RMC" and len(f) >= 9:
                self.speed_mps = float(f[7]) * 0.514444 if f[7] else None
                self.course_deg = float(f[8]) if f[8] else None
            elif kind == "GSA" and len(f) >= 16:
                self.pdop = float(f[15]) if f[15] else None
        except ValueError:
            self.bad_sentences += 1
        return None
