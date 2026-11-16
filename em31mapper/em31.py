"""Geonics EM31-MK2 serial stream decoding.

Record layout (from the Geonics EM31win manual, Appendix A):
    <info byte><reading 1: sign + 4 digits><reading 2: sign + 4 digits>
e.g.  0xA4 "-2559" "-2563"

Info byte bits:
    7  always 1
    6  marker / trigger pressed
    5  dipole mode (1 = vertical, 0 = horizontal)
    2  range bit "Range 3"
    1  range bit "Range 2"

Range (Range2, Range3):  1000 -> (1,1)   100 -> (0,1)   10 -> (1,0)

Reading 1 = conductivity (OPER / "both" mode) or in-phase (COMP / in-phase mode)
Reading 2 = in-phase (OPER mode), unused in COMP mode.
Value = raw * factor.  Short boom (EM31-SH): divide in-phase by 3.35.

The 0x80-0xFF info byte can never be an ASCII digit or sign, so the stream
self-synchronises by pattern matching even if bytes are dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

RECORD_RE = re.compile(rb"([\x80-\xff])([+-][0-9]{4})([+-][0-9]{4})")

COND_FACTOR = {1000: -0.25, 100: -0.025, 10: -0.0025}
INPHASE_FACTOR_OPER = -0.025  # same for all ranges in OPER (both components) mode
INPHASE_FACTOR_COMP = {1000: -0.0625, 100: -0.00625, 10: -0.000625}
SHORT_BOOM_INPHASE_DIVISOR = 3.35

_RANGE_FROM_BITS = {(1, 1): 1000, (0, 1): 100, (1, 0): 10}


@dataclass
class EM31Reading:
    t: float                 # PC monotonic time of arrival (s)
    info: int                # raw info byte
    raw1: int
    raw2: int
    range: int | None        # 10 / 100 / 1000, None if unrecognised
    vertical: bool
    marker: bool
    cond: float | None       # mS/m
    inphase: float | None    # ppt


def decode_info(b: int) -> tuple[int | None, bool, bool]:
    marker = bool(b & 0x40)
    vertical = bool(b & 0x20)
    r3 = 1 if b & 0x04 else 0
    r2 = 1 if b & 0x02 else 0
    return _RANGE_FROM_BITS.get((r2, r3)), vertical, marker


def encode_record(cond: float, inphase: float, rng: int = 100,
                  vertical: bool = True, marker: bool = False) -> bytes:
    """Build a record in instrument format (used by the simulator and tests)."""
    bits = {1000: 0x06, 100: 0x04, 10: 0x02}[rng]
    info = 0x80 | bits | (0x20 if vertical else 0) | (0x40 if marker else 0)
    r1 = max(-9999, min(9999, round(cond / COND_FACTOR[rng])))
    r2 = max(-9999, min(9999, round(inphase / INPHASE_FACTOR_OPER)))
    return bytes([info]) + f"{r1:+05d}{r2:+05d}".encode("ascii")


class EM31Parser:
    """Incremental parser: feed() arbitrary chunks, get complete readings back."""

    def __init__(self, component: str = "both", short_boom: bool = False):
        self.component = component      # "both" (OPER) or "inphase" (COMP)
        self.short_boom = short_boom
        self._buf = b""
        self.bad_bytes = 0

    def feed(self, data: bytes, t: float) -> list[EM31Reading]:
        self._buf += data
        out: list[EM31Reading] = []
        last_end = 0
        for m in RECORD_RE.finditer(self._buf):
            self.bad_bytes += m.start() - last_end
            last_end = m.end()
            info = m.group(1)[0]
            raw1, raw2 = int(m.group(2)), int(m.group(3))
            out.append(self._convert(t, info, raw1, raw2))
        # keep an unfinished tail (max one record length) for the next chunk
        tail = self._buf[last_end:]
        if len(tail) > 10:
            self.bad_bytes += len(tail) - 10
            tail = tail[-10:]
        self._buf = tail
        return out

    def _convert(self, t, info, raw1, raw2) -> EM31Reading:
        rng, vertical, marker = decode_info(info)
        cond = inph = None
        if rng is not None:
            if self.component == "inphase":
                inph = raw1 * INPHASE_FACTOR_COMP[rng]
            else:
                cond = raw1 * COND_FACTOR[rng]
                inph = raw2 * INPHASE_FACTOR_OPER
            if inph is not None and self.short_boom:
                inph /= SHORT_BOOM_INPHASE_DIVISOR
        return EM31Reading(t, info, raw1, raw2, rng, vertical, marker, cond, inph)
