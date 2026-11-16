"""Planned survey lines: loading and line guidance.

Supported inputs
  GPX       tracks, routes, waypoints
  KML       LineStrings, Polygons, Points
  GeoJSON   LineString / MultiLineString / Polygon / MultiPolygon / Point features;
            honours the "crs" member (CRS84, EPSG:4326, UTM EPSG:326xx/327xx/269xx)
  GeoPackage (.gpkg)  every feature table is a layer; any of the CRSs above
  CSV       lat/lon or easting/northing columns; a line column (line, transect_id, id ...)
            groups vertices; an 'end' column (start/end) orders them

Point layers whose features share a line id (e.g. transect endpoints with
transect_id + start/end) are assembled into lines. Polygons are survey
boundaries: drawn, never used for guidance.
"""
from __future__ import annotations

import csv
import json
import math
import os
import pathlib
import sqlite3
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import numpy as np

from .geo import epsg_from_name, epsg_to_latlon, to_utm, utm_zone

LINE_KEYS = ("line", "line_name", "line_id", "transect_id", "transect", "line_no", "tid")
NAME_KEYS = ("name", "line", "line_name", "line_id", "transect_id", "transect", "label", "id", "fid")
ORDER_KEYS = ("seq", "order", "vertex", "vertex_index", "point_index", "idx")
END_ORDER = {"start": 0, "begin": 0, "s": 0, "a": 0, "1": 0, "end": 1, "stop": 1, "e": 1, "b": 1, "2": 1}
TURN_DEG = 30.0           # direction change that counts as a turn on a multi-leg track


class PlanError(Exception):
    pass


# ---------------------------------------------------------------------------
# Layers: what a file contains, before the user picks what to show
# ---------------------------------------------------------------------------
@dataclass
class Layer:
    name: str
    lines: list = field(default_factory=list)      # (name, [(lat, lon), ...])
    points: list = field(default_factory=list)     # (name, lat, lon)
    polygons: list = field(default_factory=list)   # (name, [ring [(lat, lon), ...], ...])
    source: str = ""

    @property
    def kind(self):
        kinds = [k for k, v in (("lines", self.lines), ("points", self.points),
                                ("polygons", self.polygons)) if v]
        return "+".join(kinds) or "empty"

    def describe(self):
        parts = []
        if self.lines:
            n = len(self.lines)
            parts.append(f"{n} line" + ("s" if n != 1 else ""))
        if self.points:
            parts.append(f"{len(self.points)} points")
        if self.polygons:
            parts.append(f"{len(self.polygons)} boundary polygon" + ("s" if len(self.polygons) != 1 else ""))
        return ", ".join(parts) or "no geometry"


def default_selection(layers: list[Layer]) -> list[bool]:
    """Sensible defaults for a multi-layer file: boundaries + the per-line layer.

    A single long track (e.g. a serpentine route) or loose points are left off
    when a layer of individual lines exists, so lines aren't shown twice.
    """
    has_multi_lines = any(len(l.lines) >= 2 for l in layers)
    sel = []
    for l in layers:
        if l.polygons and not l.lines:
            sel.append(True)
        elif l.lines and (len(l.lines) >= 2 or not has_multi_lines):
            sel.append(not (l.source == "points" and any(len(o.lines) >= 2 and o.source != "points"
                                                           for o in layers)))
        elif l.points and not l.lines:
            sel.append(not has_multi_lines)
        else:
            sel.append(False)
    if not any(sel) and layers:
        sel[0] = True
    return sel


# ---------------------------------------------------------------------------
def _strip_ns(root):
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _feature_name(props, i, prefix="L"):
    for k in NAME_KEYS:
        for pk, v in props.items():
            if pk.lower() == k and v not in (None, ""):
                if isinstance(v, float) and v.is_integer():
                    v = int(v)
                return str(v)
    return f"{prefix}{i + 1}"


def _points_to_lines(points_with_props):
    """[(name, lat, lon, props)] -> (lines, leftover points) using shared line ids."""
    groups: dict[str, list] = {}
    order: list[str] = []
    rest = []
    for i, (nm, la, lo, props) in enumerate(points_with_props):
        low = {k.lower(): v for k, v in props.items()}
        key = next((low[k] for k in LINE_KEYS if low.get(k) not in (None, "")), None)
        if key is None:
            rest.append((nm, la, lo)); continue
        if isinstance(key, float) and key.is_integer():
            key = int(key)
        key = str(key)
        endv = str(low.get("end", low.get("position", ""))).strip().lower()
        rank = END_ORDER.get(endv)
        seq = next((low[k] for k in ORDER_KEYS if k in low), None)
        sort_key = (rank if rank is not None else 0, float(seq) if seq is not None else 0, i)
        if key not in groups:
            groups[key] = []; order.append(key)
        groups[key].append((sort_key, la, lo))
    lines = []
    for k in order:
        pts = sorted(groups[k])
        if len(pts) >= 2:
            lines.append((k, [(la, lo) for _, la, lo in pts]))
        else:
            rest.append((k, pts[0][1], pts[0][2]))
    return lines, rest


def _check_lonlat(lon, lat, what):
    if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
        raise PlanError(f"{what} has coordinates that aren't latitude/longitude "
                        f"({lon:.1f}, {lat:.1f}) and no coordinate system is declared.")


# ---------------------------------------------------------------------------
# Individual formats -> [Layer]
# ---------------------------------------------------------------------------
def read_gpx(path):
    root = _strip_ns(ET.parse(path).getroot())
    lay = Layer(os.path.splitext(os.path.basename(path))[0])
    for i, trk in enumerate(root.iter("trk")):
        name = (trk.findtext("name") or f"T{i + 1}").strip()
        segs = trk.findall("trkseg")
        for j, seg in enumerate(segs):
            ll = [(float(p.get("lat")), float(p.get("lon"))) for p in seg.findall("trkpt")]
            lay.lines.append((name if len(segs) == 1 else f"{name}.{j + 1}", ll))
    for i, rte in enumerate(root.iter("rte")):
        name = (rte.findtext("name") or f"R{i + 1}").strip()
        lay.lines.append((name, [(float(p.get("lat")), float(p.get("lon"))) for p in rte.findall("rtept")]))
    for w in root.iter("wpt"):
        lay.points.append(((w.findtext("name") or "").strip(), float(w.get("lat")), float(w.get("lon"))))
    return [lay]


def _kml_coords(text):
    out = []
    for tok in (text or "").split():
        parts = tok.split(",")
        if len(parts) >= 2:
            out.append((float(parts[1]), float(parts[0])))
    return out


def read_kml(path):
    root = _strip_ns(ET.parse(path).getroot())
    lay = Layer(os.path.splitext(os.path.basename(path))[0])
    for i, pm in enumerate(root.iter("Placemark")):
        name = (pm.findtext("name") or f"L{i + 1}").strip()
        for k, ls in enumerate(pm.iter("LineString")):
            lay.lines.append((name if k == 0 else f"{name}.{k + 1}", _kml_coords(ls.findtext("coordinates"))))
        for poly in pm.iter("Polygon"):
            rings = [_kml_coords(r.findtext("coordinates")) for r in poly.iter("LinearRing")]
            lay.polygons.append((name, [r for r in rings if len(r) >= 3]))
        for p in pm.iter("Point"):
            c = _kml_coords(p.findtext("coordinates"))
            if c:
                lay.points.append((name, *c[0]))
    return [lay]


def _geometry_to_layer(lay, geom, props, i, conv, pts_props, what):
    """Add one GeoJSON-style geometry (coords in the source CRS) to a layer."""
    if not geom:
        return
    t = geom.get("type")
    c = geom.get("coordinates")

    def ll(xy):
        lat, lon = conv(float(xy[0]), float(xy[1]))
        return lat, lon

    if t == "LineString":
        lay.lines.append((_feature_name(props, i), [ll(p) for p in c]))
    elif t == "MultiLineString":
        nm = _feature_name(props, i)
        for k, part in enumerate(c):
            lay.lines.append((nm if len(c) == 1 else f"{nm}.{k + 1}", [ll(p) for p in part]))
    elif t == "Polygon":
        lay.polygons.append((_feature_name(props, i, "B"), [[ll(p) for p in ring] for ring in c]))
    elif t == "MultiPolygon":
        nm = _feature_name(props, i, "B")
        for part in c:
            lay.polygons.append((nm, [[ll(p) for p in ring] for ring in part]))
    elif t == "Point":
        la, lo = ll(c)
        pts_props.append((_feature_name(props, i, "P"), la, lo, props))
    elif t == "MultiPoint":
        for p in c:
            la, lo = ll(p)
            pts_props.append((_feature_name(props, i, "P"), la, lo, props))
    elif t == "GeometryCollection":
        for g in geom.get("geometries", []):
            _geometry_to_layer(lay, g, props, i, conv, pts_props, what)


def _finish_points(lay, pts_props):
    if pts_props:
        lines, rest = _points_to_lines(pts_props)
        if lines:
            lay.lines.extend(lines)
            lay.source = "points"
        lay.points.extend(rest)


def read_geojson(path):
    with open(path, encoding="utf-8-sig") as f:
        gj = json.load(f)
    if isinstance(gj, dict) and "type" not in gj:
        if {"center_lat", "center_lon"} & set(gj) or "transects" in gj:
            bits = []
            if "transects" in gj:
                bits.append(f"{gj['transects']} transects")
            if "spacing_m" in gj:
                bits.append(f"{gj['spacing_m']:g} m spacing")
            if "area_acres" in gj:
                bits.append(f"{gj['area_acres']:.2f} acres")
            raise PlanError("This is a survey summary (" + ", ".join(bits) + ") with no map geometry. "
                            "Load the transects GeoJSON, the endpoints CSV or the .gpkg instead.")
        raise PlanError("This JSON file isn't GeoJSON (no \"type\").")
    code = 4326
    crs = gj.get("crs") if isinstance(gj, dict) else None
    if crs:
        nm = (crs.get("properties") or {}).get("name", "")
        code = epsg_from_name(nm) or 4326
    try:
        conv = epsg_to_latlon(code)
    except ValueError as e:
        raise PlanError(str(e))
    feats = gj.get("features")
    if feats is None:
        feats = [gj] if gj.get("type") == "Feature" else [{"geometry": gj, "properties": {}}]
    lay = Layer(gj.get("name") or os.path.splitext(os.path.basename(path))[0])
    pts_props = []
    for i, ft in enumerate(feats):
        _geometry_to_layer(lay, ft.get("geometry"), ft.get("properties") or {}, i, conv, pts_props,
                           os.path.basename(path))
    _finish_points(lay, pts_props)
    if code == 4326:                       # no declared CRS: sanity-check the numbers
        for nm, ll in lay.lines[:1]:
            _check_lonlat(ll[0][1], ll[0][0], os.path.basename(path))
        for nm, la, lo in lay.points[:1]:
            _check_lonlat(lo, la, os.path.basename(path))
    return [lay]


# --- GeoPackage ----------------------------------------------------------------
def _parse_wkb(buf, off=0):
    """Return (geojson-like dict, new offset). Handles 2D/Z/M/ZM, ISO and EWKB codes."""
    bo = "<" if buf[off] == 1 else ">"
    (code,) = struct.unpack_from(bo + "I", buf, off + 1)
    off += 5
    has_z = has_m = False
    if code & 0x80000000:
        has_z = True
    if code & 0x40000000:
        has_m = True
    if code & 0x20000000:                   # EWKB SRID present
        off += 4
    code &= 0x0FFFFFFF
    if code >= 3000:
        has_z = has_m = True; code -= 3000
    elif code >= 2000:
        has_m = True; code -= 2000
    elif code >= 1000:
        has_z = True; code -= 1000
    dims = 2 + has_z + has_m
    fmt = bo + "d" * dims
    step = 8 * dims

    def pt():
        nonlocal off
        v = struct.unpack_from(fmt, buf, off); off += step
        return [v[0], v[1]]

    def seq():
        nonlocal off
        (n,) = struct.unpack_from(bo + "I", buf, off); off += 4
        return [pt() for _ in range(n)]

    def rings():
        nonlocal off
        (n,) = struct.unpack_from(bo + "I", buf, off); off += 4
        return [seq() for _ in range(n)]

    if code == 1:
        return {"type": "Point", "coordinates": pt()}, off
    if code == 2:
        return {"type": "LineString", "coordinates": seq()}, off
    if code == 3:
        return {"type": "Polygon", "coordinates": rings()}, off
    if code in (4, 5, 6, 7):
        (n,) = struct.unpack_from(bo + "I", buf, off); off += 4
        parts = []
        for _ in range(n):
            g, off = _parse_wkb(buf, off)
            parts.append(g)
        if code == 7:
            return {"type": "GeometryCollection", "geometries": parts}, off
        kind = {4: "MultiPoint", 5: "MultiLineString", 6: "MultiPolygon"}[code]
        return {"type": kind, "coordinates": [p["coordinates"] for p in parts]}, off
    raise PlanError(f"Unsupported geometry type code {code} in GeoPackage")


def gpkg_geometry(blob):
    b = bytes(blob)
    if b[:2] != b"GP":
        return _parse_wkb(b)[0]                 # plain WKB (some writers)
    flags = b[3]
    if flags & 0x10:                            # empty geometry
        return None
    env = (flags >> 1) & 0x07
    off = 8 + {0: 0, 1: 32, 2: 48, 3: 48, 4: 64}.get(env, 0)
    return _parse_wkb(b, off)[0]


def read_gpkg(path):
    uri = pathlib.Path(os.path.abspath(path)).as_uri() + "?mode=ro&immutable=1"
    try:
        con = sqlite3.connect(uri, uri=True)
        tables = con.execute(
            "SELECT c.table_name, g.column_name, g.srs_id, COALESCE(s.organization_coordsys_id, g.srs_id), "
            "UPPER(COALESCE(s.organization, '')) "
            "FROM gpkg_contents c JOIN gpkg_geometry_columns g ON g.table_name = c.table_name "
            "LEFT JOIN gpkg_spatial_ref_sys s ON s.srs_id = g.srs_id "
            "WHERE c.data_type = 'features' ORDER BY c.rowid").fetchall()
    except sqlite3.DatabaseError as e:
        raise PlanError(f"Not a readable GeoPackage: {e}")
    if not tables:
        raise PlanError("This GeoPackage has no feature layers.")
    layers = []
    for table, gcol, srs_id, org_id, org in tables:
        code = int(org_id) if org in ("EPSG", "") and org_id not in (None, 0, -1) else 4326
        try:
            conv = epsg_to_latlon(code)
        except ValueError as e:
            raise PlanError(f"Layer '{table}': {e}")
        cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
        attr = [c for c in cols if c != gcol]
        sel = ", ".join(f'"{c}"' for c in attr + [gcol])
        lay = Layer(table)
        pts_props = []
        for i, row in enumerate(con.execute(f'SELECT {sel} FROM "{table}"')):
            props = dict(zip(attr, row[:-1]))
            if row[-1] is None:
                continue
            _geometry_to_layer(lay, gpkg_geometry(row[-1]), props, i, conv, pts_props, table)
        _finish_points(lay, pts_props)
        layers.append(lay)
    con.close()
    return layers


# --- CSV -------------------------------------------------------------------------
_LAT = ("lat", "latitude", "y_lat", "lat_dd")
_LON = ("lon", "long", "longitude", "lng", "x_lon", "lon_dd")
_E = ("east", "easting", "e", "x", "utm_e", "utm_east")
_N = ("north", "northing", "n", "y", "utm_n", "utm_north")
_LINE = LINE_KEYS + ("name", "id")


def read_csv(path, zone=None, north=True):
    """CSV with lat/lon or easting/northing columns; optional line column splits lines.

    Rows without a line column become one line per run of points; a blank row
    also starts a new line. An 'end' column (start/end) orders each line's points.
    UTM needs `zone` (or a utm_zone column).
    """
    from .geo import from_utm
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if not rows:
        raise PlanError("empty file")
    hdr = [h.strip().lower() for h in rows[0]]

    def col(names):
        for nm in names:
            if nm in hdr:
                return hdr.index(nm)
        return None

    ilat, ilon, ie, inn = col(_LAT), col(_LON), col(_E), col(_N)
    iline, izone, iend = col(_LINE), col(("utm_zone", "zone")), col(("end", "position"))
    iseq = col(ORDER_KEYS)
    if ilat is None and ie is None:
        raise PlanError("CSV needs latitude/longitude or easting/northing columns")
    groups: dict[str, list] = {}
    order: list[str] = []
    auto = 1
    for k, r in enumerate(rows[1:]):
        if not r or all(not c.strip() for c in r):
            auto += 1
            continue
        try:
            if ilat is not None and r[ilat].strip():
                la, lo = float(r[ilat]), float(r[ilon])
            else:
                z = int(r[izone]) if izone is not None and r[izone].strip() else zone
                if z is None:
                    raise PlanError("UTM coordinates need a zone (add a utm_zone column)")
                la, lo = from_utm(float(r[ie]), float(r[inn]), z, north)
        except (ValueError, IndexError):
            continue
        key = r[iline].strip() if iline is not None else str(auto)
        rank = END_ORDER.get(r[iend].strip().lower(), 0) if iend is not None else 0
        seq = float(r[iseq]) if iseq is not None and r[iseq].strip() else 0.0
        if key not in groups:
            groups[key] = []; order.append(key)
        groups[key].append(((rank, seq, k), la, lo))
    lay = Layer(os.path.splitext(os.path.basename(path))[0])
    for key in order:
        pts = sorted(groups[key])
        if len(pts) >= 2:
            lay.lines.append((key, [(la, lo) for _, la, lo in pts]))
        else:
            lay.points.append((key, pts[0][1], pts[0][2]))
    return [lay]


READERS = {".gpx": read_gpx, ".kml": read_kml, ".geojson": read_geojson, ".json": read_geojson,
           ".gpkg": read_gpkg}
FILE_FILTER = "Survey plans (*.gpkg *.geojson *.json *.gpx *.kml *.csv);;All files (*)"


def read_layers(path, zone=None) -> list[Layer]:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        layers = read_csv(path, zone)
    elif ext in READERS:
        layers = READERS[ext](path)
    else:
        raise PlanError(f"Unsupported file type {ext} (use GeoPackage, GeoJSON, GPX, KML or CSV)")
    for l in layers:
        l.lines = [(n, ll) for n, ll in l.lines if len(ll) >= 2]
        l.polygons = [(n, [r for r in rings if len(r) >= 3]) for n, rings in l.polygons]
        l.polygons = [p for p in l.polygons if p[1]]
    if not any(l.lines or l.points or l.polygons for l in layers):
        raise PlanError("No lines, points or boundaries found in the file")
    return layers


def plan_from_layers(layers: list[Layer]) -> "Plan":
    lines, points, polys = [], [], []
    for l in layers:
        lines += l.lines; points += l.points; polys += l.polygons
    if not (lines or points or polys):
        raise PlanError("Nothing selected to load")
    return Plan(lines, points, polys)


def load_plan(path, zone=None, layers=None):
    """Load a file; `layers` = list of layer names to keep (default: sensible selection)."""
    ls = read_layers(path, zone)
    if layers is None:
        keep = [l for l, s in zip(ls, default_selection(ls)) if s]
    else:
        keep = [l for l in ls if l.name in layers]
    return plan_from_layers(keep)


# ---------------------------------------------------------------------------
@dataclass
class PlanLine:
    name: str
    lat: np.ndarray
    lon: np.ndarray
    e: np.ndarray = field(default=None)
    n: np.ndarray = field(default=None)
    length: float = 0.0
    cum: np.ndarray = field(default=None)       # chainage at each vertex
    turns: np.ndarray = field(default=None)     # chainage of turn vertices


@dataclass
class Guidance:
    line: int
    name: str
    offset: float        # metres; + = you are RIGHT of the line (steer left)
    along: float         # metres travelled along the line in your direction
    remaining: float     # metres to the end in your direction
    reversed: bool       # walking the line end -> start
    to_turn: float = 0.0     # metres to the next turn (multi-leg tracks); = remaining if none
    has_turns: bool = False


class Plan:
    def __init__(self, lines, points, polygons=()):
        self.lines = [PlanLine(str(n), np.array([p[0] for p in ll], float), np.array([p[1] for p in ll], float))
                      for n, ll in lines]
        self.points = list(points)            # (name, lat, lon)
        self.polygons = list(polygons)        # (name, [ring [(lat, lon)...]])
        self.poly_en = []                     # [(name, [(e[], n[]) per ring])]
        self.zone = None
        self.north = True
        self.pe = self.pn = None

    def center_latlon(self):
        la = [l.lat for l in self.lines] + [np.array([p[1] for p in self.points])]
        lo = [l.lon for l in self.lines] + [np.array([p[2] for p in self.points])]
        for _, rings in self.polygons:
            la.append(np.array([p[0] for p in rings[0]])); lo.append(np.array([p[1] for p in rings[0]]))
        la, lo = np.concatenate(la), np.concatenate(lo)
        return float(la.mean()), float(lo.mean())

    def project(self, zone=None, north=None):
        if zone is None:
            la, lo = self.center_latlon()
            zone, north = utm_zone(la, lo), la >= 0
        self.zone, self.north = zone, north
        for ln in self.lines:
            xy = np.array([to_utm(a, b, zone, north) for a, b in zip(ln.lat, ln.lon)])
            ln.e, ln.n = xy[:, 0], xy[:, 1]
            seg = np.hypot(np.diff(ln.e), np.diff(ln.n))
            ln.cum = np.concatenate([[0.0], np.cumsum(seg)])
            ln.length = float(ln.cum[-1])
            ln.turns = self._turn_chainages(ln, seg)
        if self.points:
            xy = np.array([to_utm(p[1], p[2], zone, north) for p in self.points])
            self.pe, self.pn = xy[:, 0], xy[:, 1]
        self.poly_en = []
        for nm, rings in self.polygons:
            out = []
            for ring in rings:
                xy = np.array([to_utm(a, b, zone, north) for a, b in ring])
                out.append((xy[:, 0], xy[:, 1]))
            self.poly_en.append((nm, out))
        return self

    @staticmethod
    def _turn_chainages(ln, seg):
        if len(ln.e) < 3:
            return np.array([])
        h = np.degrees(np.arctan2(np.diff(ln.e), np.diff(ln.n)))
        dh = np.abs((np.diff(h) + 180) % 360 - 180)
        idx = np.nonzero((dh > TURN_DEG) & (seg[:-1] > 0) & (seg[1:] > 0))[0] + 1
        return ln.cum[idx]

    def bounds(self):
        es = [l.e for l in self.lines] + ([self.pe] if self.pe is not None else [])
        ns = [l.n for l in self.lines] + ([self.pn] if self.pn is not None else [])
        for _, rings in self.poly_en:
            for e, n in rings:
                es.append(e); ns.append(n)
        e, n = np.concatenate(es), np.concatenate(ns)
        return e.min(), e.max(), n.min(), n.max()

    # -- guidance --------------------------------------------------------------
    @staticmethod
    def _project_onto(line: PlanLine, e, n):
        """Closest point: returns (distance, signed offset (+right of start->end), chainage, segment)."""
        ax, ay = line.e[:-1], line.n[:-1]
        dx, dy = np.diff(line.e), np.diff(line.n)
        L2 = dx * dx + dy * dy
        L2s = np.where(L2 > 0, L2, 1)
        t = np.clip(((e - ax) * dx + (n - ay) * dy) / L2s, 0, 1)
        px, py = ax + t * dx, ay + t * dy
        d = np.hypot(e - px, n - py)
        k = int(np.argmin(d))
        seglen = np.sqrt(L2)
        cross = dx[k] * (n - ay[k]) - dy[k] * (e - ax[k])    # >0 : left of travel
        offset = -cross / seglen[k] if seglen[k] > 0 else 0.0
        chain = float(seglen[:k].sum() + t[k] * seglen[k])
        return float(d[k]), float(offset), chain, k

    def nearest(self, e, n) -> int | None:
        if not self.lines:
            return None
        return int(np.argmin([self._project_onto(l, e, n)[0] for l in self.lines]))

    def guidance(self, idx, e, n, heading_deg=None) -> Guidance | None:
        if idx is None or not (0 <= idx < len(self.lines)):
            return None
        ln = self.lines[idx]
        _, off, chain, k = self._project_onto(ln, e, n)
        rev = False
        if heading_deg is not None:
            sx, sy = ln.e[k + 1] - ln.e[k], ln.n[k + 1] - ln.n[k]
            h = math.radians(heading_deg)
            rev = (sx * math.sin(h) + sy * math.cos(h)) < 0
        turns = ln.turns if ln.turns is not None else np.array([])
        if rev:
            prev = turns[turns < chain - 1e-6]
            to_turn = chain - prev.max() if len(prev) else chain
            return Guidance(idx, ln.name, -off, ln.length - chain, chain, True, float(to_turn), len(turns) > 0)
        nxt = turns[turns > chain + 1e-6]
        to_turn = nxt.min() - chain if len(nxt) else ln.length - chain
        return Guidance(idx, ln.name, off, chain, ln.length - chain, False, float(to_turn), len(turns) > 0)


def write_test_gpkg(path):
    """Tiny GeoPackage (WGS84 line + polygon) used by the self-test and unit tests."""
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE gpkg_spatial_ref_sys(srs_name TEXT, srs_id INTEGER PRIMARY KEY, organization TEXT,
            organization_coordsys_id INTEGER, definition TEXT, description TEXT);
        INSERT INTO gpkg_spatial_ref_sys VALUES('WGS 84', 4326, 'EPSG', 4326, '', '');
        CREATE TABLE gpkg_contents(table_name TEXT PRIMARY KEY, data_type TEXT, identifier TEXT,
            description TEXT, last_change TEXT, min_x REAL, min_y REAL, max_x REAL, max_y REAL, srs_id INTEGER);
        CREATE TABLE gpkg_geometry_columns(table_name TEXT, column_name TEXT, geometry_type_name TEXT,
            srs_id INTEGER, z INTEGER, m INTEGER);
        CREATE TABLE lines(fid INTEGER PRIMARY KEY, geom BLOB, name TEXT);
        CREATE TABLE area(fid INTEGER PRIMARY KEY, geom BLOB, name TEXT);
        INSERT INTO gpkg_contents(table_name, data_type, srs_id) VALUES('area', 'features', 4326);
        INSERT INTO gpkg_contents(table_name, data_type, srs_id) VALUES('lines', 'features', 4326);
        INSERT INTO gpkg_geometry_columns VALUES('area', 'geom', 'POLYGON', 4326, 0, 0);
        INSERT INTO gpkg_geometry_columns VALUES('lines', 'geom', 'LINESTRING', 4326, 0, 0);
    """)
    hdr = b"GP\x00\x01" + struct.pack("<i", 4326)
    line = struct.pack("<BII", 1, 2, 2) + struct.pack("<4d", -117.2367, 47.7804, -117.2347, 47.7804)
    ring = [(-117.2368, 47.7803), (-117.2346, 47.7803), (-117.2346, 47.7806), (-117.2368, 47.7806),
            (-117.2368, 47.7803)]
    poly = struct.pack("<BIII", 1, 3, 1, len(ring)) + b"".join(struct.pack("<2d", *p) for p in ring)
    con.execute("INSERT INTO lines(geom, name) VALUES(?, ?)", (hdr + line, "T1"))
    con.execute("INSERT INTO area(geom, name) VALUES(?, ?)", (hdr + poly, "boundary"))
    con.commit()
    con.close()
