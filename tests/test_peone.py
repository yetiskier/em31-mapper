"""Real survey-plan files in every format they were supplied in (Peone 3 m EM31 survey).

The files live in tests/fixtures/peone/ and are not distributed with the source;
these tests skip when they are absent.
"""
import csv
import os

import numpy as np
import pytest

from em31mapper.plan import PlanError, default_selection, load_plan, read_layers

D = os.path.join(os.path.dirname(__file__), "fixtures", "peone")
P = lambda f: os.path.join(D, f)

pytestmark = pytest.mark.skipif(not os.path.exists(P("peone_em31_3m_survey_endpoints.csv")),
                                reason="Peone survey fixtures not present")

# reference: 28 transects, start/end easting-northing straight from the survey CSV (UTM 11N)
REF = {}
if os.path.exists(P("peone_em31_3m_survey_endpoints.csv")):
    with open(P("peone_em31_3m_survey_endpoints.csv")) as f:
        for r in csv.DictReader(f):
            REF.setdefault(r["transect_id"], {})[r["end"]] = (float(r["easting"]), float(r["northing"]))


def check_transects(plan):
    assert plan.zone == 11 and plan.north
    names = [l.name for l in plan.lines]
    assert names == [str(i) for i in range(1, 29)], names
    for ln in plan.lines:
        s, e = REF[ln.name]["start"], REF[ln.name]["end"]
        assert abs(ln.e[0] - s[0]) < 0.005 and abs(ln.n[0] - s[1]) < 0.005, ln.name
        assert abs(ln.e[-1] - e[0]) < 0.005 and abs(ln.n[-1] - e[1]) < 0.005, ln.name
    total = sum(l.length for l in plan.lines)
    assert abs(total - 3485.0619) < 0.1          # data_transect_length_m in the summary


@pytest.mark.parametrize("fname", [
    "peone_em31_3m_survey_transects_3m_EW.geojson",
    "peone_em31_3m_survey_transect_endpoints.geojson",   # points -> lines via transect_id/end
    "peone_em31_3m_survey_endpoints.csv",
])
def test_single_files_give_28_transects(fname):
    check_transects(load_plan(P(fname)).project())


def test_gpkg_layers_and_defaults():
    layers = read_layers(P("peone_em31_3m_survey.gpkg"))
    info = {l.name: l.describe() for l in layers}
    assert info == {"boundary": "1 boundary polygon", "transects_3m_EW": "28 lines",
                    "serpentine_track": "1 line", "transect_endpoints": "28 lines"}
    sel = dict(zip([l.name for l in layers], default_selection(layers)))
    assert sel == {"boundary": True, "transects_3m_EW": True,
                   "serpentine_track": False, "transect_endpoints": False}
    plan = load_plan(P("peone_em31_3m_survey.gpkg")).project()
    check_transects(plan)
    assert len(plan.poly_en) == 1
    # boundary polygon matches the GeoJSON boundary
    b = load_plan(P("peone_em31_3m_survey_boundary.geojson")).project(11, True)
    ge, gn = plan.poly_en[0][1][0]
    je, jn = b.poly_en[0][1][0]
    assert np.allclose(ge, je, atol=0.01) and np.allclose(gn, jn, atol=0.01)


def test_gpkg_choose_layers():
    plan = load_plan(P("peone_em31_3m_survey.gpkg"), layers=["serpentine_track"]).project()
    assert len(plan.lines) == 1 and abs(plan.lines[0].length - 3693.604) < 0.1


def test_serpentine_turn_guidance():
    plan = load_plan(P("peone_em31_3m_survey_serpentine_track.geojson")).project()
    ln = plan.lines[0]
    assert abs(ln.length - 3693.604) < 0.1          # total_track_length_m in the file
    assert len(ln.turns) >= 50                        # every transect end is a turn
    # stand 10 m along transect 3 (W->E), walking east
    s = REF["3"]["start"]
    g = plan.guidance(0, s[0] + 10, s[1], heading_deg=90)
    assert g.has_turns and not g.reversed and abs(g.offset) < 0.05
    leg = REF["3"]["end"][0] - s[0]
    assert abs(g.to_turn - (leg - 10)) < 0.05       # counts down to the end of this leg
    assert g.remaining > 3000                          # but the whole track is still ahead


def test_boundary_only():
    plan = load_plan(P("peone_em31_3m_survey_boundary.geojson")).project()
    assert not plan.lines and len(plan.poly_en) == 1
    e0, e1, n0, n1 = plan.bounds()
    assert 150 < e1 - e0 < 160 and 80 < n1 - n0 < 85


def test_summary_json_explains_itself():
    with pytest.raises(PlanError, match="28 transects, 3 m spacing, 2.58 acres"):
        load_plan(P("peone_em31_3m_survey_summary.json"))


def test_projected_geojson_crs(tmp_path):
    # the same transect written in UTM 11N with an EPSG crs member
    s, e = REF["3"]["start"], REF["3"]["end"]
    gj = ('{"type":"FeatureCollection","crs":{"type":"name","properties":{"name":"urn:ogc:def:crs:EPSG::32611"}},'
          '"features":[{"type":"Feature","properties":{"id":3},"geometry":{"type":"LineString",'
          f'"coordinates":[[{s[0]},{s[1]}],[{e[0]},{e[1]}]]}}}}]}}')
    (tmp_path / "utm.geojson").write_text(gj)
    ln = load_plan(str(tmp_path / "utm.geojson")).project().lines[0]
    assert abs(ln.e[0] - s[0]) < 0.005 and abs(ln.n[-1] - e[1]) < 0.005
    # projected numbers with no crs -> clear error, not a map in the ocean
    (tmp_path / "bad.geojson").write_text(gj.replace('"crs":{"type":"name","properties":{"name":"urn:ogc:def:crs:EPSG::32611"}},', ""))
    with pytest.raises(PlanError, match="aren't latitude/longitude"):
        load_plan(str(tmp_path / "bad.geojson"))
