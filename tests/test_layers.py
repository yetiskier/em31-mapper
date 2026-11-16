import json
import numpy as np
import pytest
from em31mapper.plan import load_plan, Plan
from em31mapper.geo import to_utm, from_utm


def _lines_utm(zone=12):
    # three N-S lines 2 m apart, 50 m long, starting at E=500000
    out = []
    for i in range(3):
        e = 500000 + 2 * i
        a = from_utm(e, 4982950, zone); b = from_utm(e, 4983000, zone)
        out.append((f"L{i+1}", [a, b]))
    return out


def test_gpx_kml_geojson_csv(tmp_path):
    lines = _lines_utm()
    gpx = '<gpx xmlns="http://www.topografix.com/GPX/1/1">' + "".join(
        f'<trk><name>{n}</name><trkseg>' + "".join(f'<trkpt lat="{a}" lon="{b}"/>' for a, b in ll)
        + "</trkseg></trk>" for n, ll in lines) + '<wpt lat="45" lon="-111"><name>BM1</name></wpt></gpx>'
    (tmp_path / "p.gpx").write_text(gpx)
    kml = '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>' + "".join(
        f"<Placemark><name>{n}</name><LineString><coordinates>"
        + " ".join(f"{b},{a},0" for a, b in ll) + "</coordinates></LineString></Placemark>"
        for n, ll in lines) + "</Document></kml>"
    (tmp_path / "p.kml").write_text(kml)
    gj = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": n},
         "geometry": {"type": "LineString", "coordinates": [[b, a] for a, b in ll]}} for n, ll in lines]}
    (tmp_path / "p.geojson").write_text(json.dumps(gj))
    rows = ["line,east,north,utm_zone"] + [
        f"{n},{to_utm(a, b, 12)[0]},{to_utm(a, b, 12)[1]},12" for n, ll in lines for a, b in ll]
    (tmp_path / "p.csv").write_text("\n".join(rows))
    for ext in ("gpx", "kml", "geojson", "csv"):
        p = load_plan(str(tmp_path / f"p.{ext}")).project()
        assert [l.name for l in p.lines] == ["L1", "L2", "L3"], ext
        assert p.zone == 12
        assert abs(p.lines[1].e[0] - 500002) < 0.01 and abs(p.lines[0].length - 50) < 0.01
    assert load_plan(str(tmp_path / "p.gpx")).points[0][0] == "BM1"


def test_guidance_signs_and_direction():
    p = Plan(_lines_utm(), []).project(12, True)
    # walking north on L2 (E=500002), 0.5 m east (right) of it, 20 m in
    g = p.guidance(1, 500002.5, 4982970, heading_deg=0)
    assert not g.reversed and abs(g.offset - 0.5) < 1e-3
    assert abs(g.along - 20) < 1e-3 and abs(g.remaining - 30) < 1e-3
    # walking south: east of the line is now your LEFT
    g = p.guidance(1, 500002.5, 4982970, heading_deg=180)
    assert g.reversed and abs(g.offset + 0.5) < 1e-3 and abs(g.remaining - 20) < 1e-3
    assert p.nearest(500003.9, 4982990) == 2


def test_geotiff_georef(tmp_path):
    tifffile = pytest.importorskip("tifffile")
    from em31mapper.imagery import read_geotiff
    img = np.zeros((100, 200, 3), np.uint8); img[:, :, 0] = 200
    # UTM 12N (EPSG 32612), 0.5 m pixels, top-left corner at (500000, 4983000)
    geokeys = [1, 1, 0, 3, 1024, 0, 1, 1, 1025, 0, 1, 1, 3072, 0, 1, 32612]
    tifffile.imwrite(tmp_path / "a.tif", img, photometric="rgb", extratags=[
        (33550, 12, 3, (0.5, 0.5, 0.0), True),
        (33922, 12, 6, (0, 0, 0, 500000.0, 4983000.0, 0), True),
        (34735, 3, len(geokeys), geokeys, True)])
    arr, ref = read_geotiff(str(tmp_path / "a.tif"))
    assert arr.shape == (100, 200, 3)
    assert ref.kind == "utm" and ref.zone == 12
    assert ref.native(0, 0) == (500000.0, 4983000.0)
    assert ref.native(200, 100) == (500100.0, 4982950.0)


def test_diff_grid_common_frame():
    from em31mapper.gridding import diff_grid
    rng = np.random.default_rng(0)
    # two passes over the same 40x40 m area at different sample positions
    ev, nv = rng.uniform(0, 40, 3000), rng.uniform(0, 40, 3000)
    eh, nh = rng.uniform(0, 40, 3000), rng.uniform(0, 40, 3000)
    vv = 10 + 0.5 * ev            # VMD
    vh = 5 + 0.5 * eh             # HMD: same gradient, 5 mS/m lower
    g, x0, y0, cell = diff_grid(ev, nv, vv, eh, nh, vh, cell=1.0, radius=3.0)
    core = g[5:-5, 5:-5]
    assert np.isfinite(core).all()
    assert abs(np.nanmedian(core) - 5.0) < 0.05 and np.nanmax(np.abs(core - 5.0)) < 0.6
    # no overlap -> no difference anywhere
    assert diff_grid(ev, nv, vv, eh + 1000, nh, vh) is None


def test_minimal_wgs84_gpkg(tmp_path):
    from em31mapper.plan import write_test_gpkg
    p = str(tmp_path / "t.gpkg")
    write_test_gpkg(p)
    plan = load_plan(p).project()
    assert plan.zone == 11 and len(plan.lines) == 1 and len(plan.poly_en) == 1
    assert 140 < plan.lines[0].length < 160


def test_utm_geojson_without_fixtures(tmp_path):
    from em31mapper.plan import PlanError
    gj = ('{"type":"FeatureCollection","crs":{"type":"name","properties":{"name":"urn:ogc:def:crs:EPSG::32611"}},'
          '"features":[{"type":"Feature","properties":{"id":7},"geometry":{"type":"LineString",'
          '"coordinates":[[482267.29,5291930.02],[482421.80,5291930.02]]}}]}')
    (tmp_path / "utm.geojson").write_text(gj)
    ln = load_plan(str(tmp_path / "utm.geojson")).project().lines[0]
    assert ln.name == "7" and abs(ln.e[0] - 482267.29) < 0.005 and abs(ln.length - 154.51) < 0.01
    (tmp_path / "bad.geojson").write_text(gj.replace('"crs":{"type":"name","properties":{"name":"urn:ogc:def:crs:EPSG::32611"}},', ""))
    with pytest.raises(PlanError, match="aren't latitude/longitude"):
        load_plan(str(tmp_path / "bad.geojson"))
