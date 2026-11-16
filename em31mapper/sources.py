"""Background byte sources: real serial ports or a built-in field simulator.

Each source runs in its own thread, timestamps every chunk with
time.monotonic() as it arrives, and pushes (kind, t, bytes) onto a queue.
Parsing happens in the GUI thread so the timestamps stay honest.
"""
from __future__ import annotations

import math
import queue
import random
import threading
import time

from .em31 import encode_record

try:
    import serial
    import serial.tools.list_ports
except ImportError:  # allows tests without pyserial
    serial = None


def list_ports() -> list[tuple[str, str]]:
    if serial is None:
        return []
    return [(p.device, f"{p.device} — {p.description}")
            for p in sorted(serial.tools.list_ports.comports(), key=lambda p: p.device)]


class SerialSource(threading.Thread):
    def __init__(self, kind: str, port: str, baud: int, out: queue.Queue):
        super().__init__(daemon=True, name=f"{kind}-serial")
        self.kind, self.port, self.baud, self.out = kind, port, baud, out
        self._stop = threading.Event()
        self.error: str | None = None
        self.connected = False

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.is_set():
            try:
                with serial.Serial(self.port, self.baud, bytesize=8, parity="N",
                                   stopbits=1, timeout=0.05) as s:
                    self.connected, self.error = True, None
                    self.out.put((self.kind + "_status", time.monotonic(), b"open"))
                    while not self._stop.is_set():
                        n = s.in_waiting
                        data = s.read(n if n else 1)
                        if data:
                            self.out.put((self.kind, time.monotonic(), data))
            except Exception as e:  # port missing, Bluetooth dropped, unplugged ...
                self.connected = False
                self.error = str(e)
                self.out.put((self.kind + "_status", time.monotonic(), str(e).encode()))
                self._stop.wait(2.0)          # retry like EM31win does


# --------------------------------------------------------------------------
# Simulator: a walker doing lawnmower lines over a synthetic conductivity field
# --------------------------------------------------------------------------
class SimWalker:
    def __init__(self, lat0=45.0, lon0=-110.0, line_len=80.0, spacing=2.0, speed=1.2):
        self.lat0, self.lon0 = lat0, lon0
        self.line_len, self.spacing, self.speed = line_len, spacing, speed
        self.t0 = time.monotonic()

    def xy(self, t):
        d = (t - self.t0) * self.speed
        per_line = self.line_len + self.spacing
        k = int(d // per_line)
        r = d - k * per_line
        if r < self.line_len:
            y = r if k % 2 == 0 else self.line_len - r
            x = k * self.spacing
        else:
            y = self.line_len if k % 2 == 0 else 0.0
            x = k * self.spacing + (r - self.line_len)
        return x, y

    def latlon(self, t):
        x, y = self.xy(t)
        lat = self.lat0 + y / 111132.0
        lon = self.lon0 + x / (111320.0 * math.cos(math.radians(self.lat0)))
        return lat, lon

    @staticmethod
    def field(x, y, vertical=True):
        if not vertical:   # HMD: shallower look — sharper near-surface features, weaker deep trend
            cond = (14 + 0.02 * y
                    + 30 * math.exp(-((x - 22) ** 2 + (y - 45) ** 2) / (2 * 5 ** 2))
                    + 25 * math.exp(-((x - 50) ** 2 + (y - 20) ** 2) / (2 * 3 ** 2)))
            inph = 0.3 + 9 * math.exp(-((x - 0.6 * y - 10) ** 2) / 1.0)
            return cond, inph
        cond = (18 + 0.08 * y
                + 45 * math.exp(-((x - 22) ** 2 + (y - 45) ** 2) / (2 * 9 ** 2))
                + 15 * math.exp(-((x - 50) ** 2 + (y - 20) ** 2) / (2 * 5 ** 2)))
        inph = 0.4 + 6 * math.exp(-((x - 0.6 * y - 10) ** 2) / 2.0)   # buried pipe
        return cond, inph


class SimSource(threading.Thread):
    def __init__(self, kind: str, walker: SimWalker, out: queue.Queue, rate: float,
                 vertical=lambda: True):
        super().__init__(daemon=True, name=f"{kind}-sim")
        self.vertical = vertical
        self.kind, self.walker, self.out, self.rate = kind, walker, out, rate
        self._stop = threading.Event()
        self.connected = True
        self.error = None

    def stop(self):
        self._stop.set()

    def run(self):
        period = 1.0 / self.rate
        nxt = time.monotonic()
        while not self._stop.is_set():
            nxt += period
            self._stop.wait(max(0.0, nxt - time.monotonic()))
            t = time.monotonic()
            if self.kind == "em":
                x, y = self.walker.xy(t)
                v = self.vertical()
                c, i = SimWalker.field(x, y, v)
                c += random.gauss(0, 0.15)
                i += random.gauss(0, 0.03)
                data = encode_record(c, i, 100, vertical=v)
                # split records randomly to exercise the parser's buffering
                cut = random.randint(0, len(data))
                self.out.put((self.kind, t, data[:cut]))
                self.out.put((self.kind, t, data[cut:]))
            else:
                lat, lon = self.walker.latlon(t)
                self.out.put((self.kind, t, _gga(lat, lon, t).encode()))


def _nmea(body: str) -> str:
    cs = 0
    for ch in body:
        cs ^= ord(ch)
    return f"${body}*{cs:02X}\r\n"


def _gga(lat, lon, t):
    hh = time.gmtime()
    utc = f"{hh.tm_hour:02d}{hh.tm_min:02d}{hh.tm_sec:02d}.{int((t % 1) * 100):02d}"
    la, lo = abs(lat), abs(lon)
    lat_s = f"{int(la):02d}{(la - int(la)) * 60:010.7f}"
    lon_s = f"{int(lo):03d}{(lo - int(lo)) * 60:010.7f}"
    body = (f"GPGGA,{utc},{lat_s},{'N' if lat >= 0 else 'S'},{lon_s},"
            f"{'E' if lon >= 0 else 'W'},4,14,0.7,1000.0,M,-15.0,M,1.0,0001")
    return _nmea(body)
