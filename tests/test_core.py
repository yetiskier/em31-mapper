import math
from em31mapper.em31 import EM31Parser, encode_record, decode_info
from em31mapper.nmea import NMEAReader
from em31mapper.fusion import Fuser
from em31mapper.geo import to_utm
from em31mapper.sources import _gga


def test_manual_example_record():
    # Record from the Geonics EM31win manual: "T\xa4-2559-2563"
    p = EM31Parser()
    out = p.feed(b"T\xa4-2559-2563 68101654\n", 1.0)
    assert len(out) == 1
    r = out[0]
    assert r.range == 100 and r.vertical and not r.marker
    assert math.isclose(r.cond, 63.975) and math.isclose(r.inphase, 64.075)


def test_ranges_and_marker():
    assert decode_info(0x80 | 0x06) == (1000, False, False)
    assert decode_info(0x80 | 0x02 | 0x40) == (10, False, True)


def test_split_chunks_and_garbage():
    data = b"xx" + b"".join(encode_record(20 + k, 0.5) for k in range(50)) + b"junk"
    p = EM31Parser()
    got = []
    for i in range(0, len(data), 7):
        got += p.feed(data[i:i + 7], i)
    assert len(got) == 50
    assert math.isclose(got[10].cond, 30.0, abs_tol=0.03)


def test_short_boom_and_comp():
    rec = encode_record(10, 2.0)
    assert math.isclose(EM31Parser(short_boom=True).feed(rec, 0)[0].inphase, 2.0 / 3.35, rel_tol=1e-3)
    r = EM31Parser(component="inphase").feed(rec, 0)[0]
    assert r.cond is None and r.inphase is not None


def test_nmea_gga_and_checksum():
    rd = NMEAReader()
    line = _gga(45.123456, -110.654321, 0.0)
    fixes, lines = rd.feed(line.encode(), 5.0)
    assert len(fixes) == 1
    assert abs(fixes[0].lat - 45.123456) < 1e-7 and abs(fixes[0].lon + 110.654321) < 1e-7
    bad = line.replace("*", "0*")
    assert rd.feed(bad.encode(), 6.0)[0] == []


def test_utm_known_point():
    # 45N, 111W -> zone 12, central meridian: E=500000, N≈4982950.4 (k0 × meridian arc 4984944.4)
    e, n = to_utm(45.0, -111.0, 12)
    assert abs(e - 500000) < 0.01 and abs(n - 4982950.40) < 0.05


def test_fusion_interpolation_and_layback():
    from em31mapper.nmea import GPSFix
    from em31mapper.em31 import EM31Reading
    f = Fuser(); f.layback = 1.0
    lat0, lon0 = 45.0, -111.0
    dlat = 10 / 111132.0           # 10 m north per second
    out = f.add_fix(GPSFix(0.0, "", lat0, lon0, 0, 4, 10, 1, ""))
    out += f.add_em(EM31Reading(0.5, 0xa4, 0, 0, 100, True, False, 1.0, 0.0))
    assert out == []               # waits for the next fix
    out = f.add_fix(GPSFix(1.0, "", lat0 + dlat, lon0, 0, 4, 10, 1, ""))
    assert len(out) == 1
    e0, n0 = to_utm(lat0, lon0, 12)
    assert abs(out[0].north - (n0 + 5.0 - 1.0)) < 0.05   # halfway, minus 1 m layback
    assert abs(out[0].east - e0) < 0.05
