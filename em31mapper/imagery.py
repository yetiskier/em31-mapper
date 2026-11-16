"""Aerial imagery: georeferenced files and an offline tile cache.

Everything is drawn in the survey's UTM zone. Each image (or block of an image)
is placed with an affine transform fitted to its corners; blocks are small
enough (one 256-px tile, or 512-px chunks of a file) that the Web Mercator /
geographic -> UTM mismatch inside a block is well under a centimetre.
"""
from __future__ import annotations

import math
import os
import re
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from PySide6 import QtCore, QtGui, QtWidgets

from .geo import (from_utm, lonlat_to_tile, merc_to_lonlat, tile_resolution,
                  tile_to_lonlat, to_utm, utm_zone)

TILE_SOURCES = {
    "USGS Imagery (public domain, US)":
        "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}",
    "USGS Topo (public domain, US)":
        "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}",
}
USER_AGENT = "EM31Mapper/0.2 (geophysical field survey tool; offline cache)"
MAX_DOWNLOAD_TILES = 20_000
MAX_SHOWN_TILES = 350


class ImageryError(Exception):
    pass


def source_key(url_template: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", url_template.split("//")[-1])[:80]


# ---------------------------------------------------------------------------
# placement helpers
# ---------------------------------------------------------------------------
def make_item(qimg: QtGui.QImage, p00, p10, p01) -> QtWidgets.QGraphicsPixmapItem:
    """Pixmap item whose pixel (0,0)->p00, (w,0)->p10, (0,h)->p01 in map coords."""
    w, h = qimg.width(), qimg.height()
    item = QtWidgets.QGraphicsPixmapItem(QtGui.QPixmap.fromImage(qimg))
    item.setTransformationMode(QtCore.Qt.SmoothTransformation)
    item.setShapeMode(QtWidgets.QGraphicsPixmapItem.BoundingRectShape)
    item.setTransform(QtGui.QTransform(
        (p10[0] - p00[0]) / w, (p10[1] - p00[1]) / w,
        (p01[0] - p00[0]) / h, (p01[1] - p00[1]) / h,
        p00[0], p00[1]))
    item.setZValue(-100)
    item.setAcceptedMouseButtons(QtCore.Qt.NoButton)
    return item


def np_to_qimage(a: np.ndarray) -> QtGui.QImage:
    a = np.ascontiguousarray(a)
    h, w = a.shape[:2]
    if a.ndim == 2:
        return QtGui.QImage(a.data, w, h, a.strides[0], QtGui.QImage.Format_Grayscale8).copy()
    if a.shape[2] == 4:
        return QtGui.QImage(a.data, w, h, a.strides[0], QtGui.QImage.Format_RGBA8888).copy()
    return QtGui.QImage(a.data, w, h, a.strides[0], QtGui.QImage.Format_RGB888).copy()


def to_uint8(a: np.ndarray) -> np.ndarray:
    if a.dtype == np.uint8:
        return a
    a = a.astype(np.float32)
    lo, hi = np.nanpercentile(a, [1, 99])
    return np.clip((a - lo) / max(hi - lo, 1e-6) * 255, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------------------
# georeferenced files
# ---------------------------------------------------------------------------
class GeoRef:
    """pixel (col,row) -> native (x,y) affine + CRS kind."""

    def __init__(self, a, b, c, d, e, f, kind, zone=None, north=True):
        # x = a*col + b*row + c ;  y = d*col + e*row + f   (col/row at pixel corners)
        self.a, self.b, self.c, self.d, self.e, self.f = a, b, c, d, e, f
        self.kind, self.zone, self.north = kind, zone, north

    def native(self, col, row):
        return self.a * col + self.b * row + self.c, self.d * col + self.e * row + self.f

    def lonlat(self, col, row):
        x, y = self.native(col, row)
        if self.kind == "geographic":
            return x, y
        if self.kind == "webmercator":
            return merc_to_lonlat(x, y)
        lat, lon = from_utm(x, y, self.zone, self.north)
        return lon, lat


def crs_from_epsg(code: int):
    if code in (4326, 4269, 4258, 4283, 4617):
        return "geographic", None, True
    if code in (3857, 900913, 3785, 102100):
        return "webmercator", None, True
    for base, north in ((32600, True), (32700, False), (26900, True), (25800, True)):
        if base < code <= base + 60:
            return "utm", code - base, north
    raise ImageryError(f"EPSG:{code} isn't supported directly. Reproject the image to "
                       "UTM (WGS84 or NAD83) or WGS84 lat/lon first (QGIS: Raster → Projections → Warp).")


def crs_from_wkt(wkt: str):
    w = wkt.upper()
    m = re.search(r"UTM[ _]ZONE[ _](\d{1,2})\s*([NS])?", w)
    if m:
        return "utm", int(m.group(1)), (m.group(2) or "N") == "N"
    m = re.search(r'AUTHORITY\["EPSG","(\d+)"\]\]\s*$', w) or re.search(r'ID\["EPSG",(\d+)\]\]\s*$', w)
    if m:
        return crs_from_epsg(int(m.group(1)))
    if "PSEUDO" in w or "AUXILIARY_SPHERE" in w or "WEB_MERCATOR" in w:
        return "webmercator", None, True
    if w.startswith("GEOGCS") or w.startswith("GEOGCRS"):
        return "geographic", None, True
    raise ImageryError("Couldn't recognise the projection in the .prj file.")


def _hw(shape):
    if len(shape) == 2:
        return shape
    if shape[-1] in (1, 3, 4) or len(shape) > 3:
        return shape[-3], shape[-2]
    return shape[-2], shape[-1]          # planar (bands, h, w)


def read_geotiff(path, max_side=6000):
    import tifffile
    with tifffile.TiffFile(path) as tif:
        geo = tif.geotiff_metadata
        if not geo:
            raise ImageryError("This TIFF has no GeoTIFF tags (is it georeferenced?).")
        series = tif.series[0]
        levels = list(getattr(series, "levels", None) or [series])
        level = next((lv for lv in levels if max(_hw(lv.shape)) <= max_side), levels[-1])
        arr = level.asarray()
        full_h = _hw(series.shape)[0]
    if arr.ndim == 3 and arr.shape[-1] not in (1, 3, 4) and arr.shape[0] in (3, 4, 5, 6, 7, 8):
        arr = np.moveaxis(arr, 0, -1)            # planar -> interleaved
    if arr.ndim == 3:
        arr = arr[:, :, 0] if arr.shape[2] == 1 else arr[:, :, :3]   # drop alpha / NIR
    step = max(1, int(math.ceil(max(arr.shape[:2]) / max_side)))
    arr = to_uint8(arr[::step, ::step])
    scale = full_h / arr.shape[0]

    def val(k):
        v = geo.get(k)
        return int(v) if v is not None and not isinstance(v, (list, tuple)) else v

    if "ModelTransformation" in geo:
        t = np.array(geo["ModelTransformation"], float).reshape(-1)
        a, b, c, d, e, f = t[0], t[1], t[3], t[4], t[5], t[7]
    else:
        tp = list(geo["ModelTiepoint"]); ps = list(geo["ModelPixelScale"])
        a, b, c = ps[0], 0.0, tp[3] - tp[0] * ps[0]
        d, e, f = 0.0, -ps[1], tp[4] + tp[1] * ps[1]
    if val("GTRasterTypeGeoKey") == 2:        # PixelIsPoint -> shift half a pixel
        c -= 0.5 * (a + b); f -= 0.5 * (d + e)
    a, b, d, e = a * scale, b * scale, d * scale, e * scale
    code = val("ProjectedCSTypeGeoKey") or val("GeographicTypeGeoKey")
    if not code or code == 32767:
        raise ImageryError("GeoTIFF uses a user-defined projection; reproject it to UTM or "
                           "WGS84 first.")
    kind, zone, north = crs_from_epsg(code)
    return arr, GeoRef(a, b, c, d, e, f, kind, zone, north)


WORLD_EXT = {".jpg": (".jgw", ".jpgw", ".wld"), ".jpeg": (".jgw", ".jpegw", ".wld"),
             ".png": (".pgw", ".pngw", ".wld"), ".tif": (".tfw", ".tifw", ".wld"),
             ".tiff": (".tfw", ".tiffw", ".wld")}


def read_world_file_image(path, assume=None, max_side=6000):
    base, ext = os.path.splitext(path)
    wf = next((base + x for x in WORLD_EXT.get(ext.lower(), ()) if os.path.exists(base + x)), None)
    if wf is None:
        raise ImageryError("No world file (.jgw/.pgw/.tfw/.wld) next to the image.")
    vals = [float(v) for v in open(wf).read().split()[:6]]
    A, D, B, E, C, F = vals                     # world file order
    if os.path.exists(base + ".prj"):
        kind, zone, north = crs_from_wkt(open(base + ".prj").read())
    elif assume:
        kind, zone, north = assume
    else:
        raise ImageryError("NEEDS_CRS")
    q = QtGui.QImage(path)
    if q.isNull():
        raise ImageryError("Couldn't read the image.")
    step = max(1, int(math.ceil(max(q.width(), q.height()) / max_side)))
    if step > 1:
        q = q.scaled(q.width() // step, q.height() // step, QtCore.Qt.IgnoreAspectRatio,
                     QtCore.Qt.SmoothTransformation)
    q = q.convertToFormat(QtGui.QImage.Format_RGB888)
    ptr = q.constBits()
    arr = np.frombuffer(ptr, np.uint8, q.sizeInBytes()).reshape(q.height(), q.bytesPerLine())
    arr = arr[:, : q.width() * 3].reshape(q.height(), q.width(), 3).copy()
    # world file refers to pixel centres
    C0, F0 = C - 0.5 * (A + B), F - 0.5 * (D + E)
    s = step
    return arr, GeoRef(A * s, B * s, C0, D * s, E * s, F0, kind, zone, north)


def place_image(arr, ref: GeoRef, zone, north, chunk=512):
    """Split into chunks, each with its own affine into the survey UTM zone."""
    h, w = arr.shape[:2]
    same = ref.kind == "utm" and ref.zone == zone and ref.north == north
    items = []
    if same:
        q = np_to_qimage(arr)
        items.append(make_item(q, ref.native(0, 0), ref.native(w, 0), ref.native(0, h)))
        return items
    for r0 in range(0, h, chunk):
        for c0 in range(0, w, chunk):
            sub = arr[r0:r0 + chunk, c0:c0 + chunk]
            sh, sw = sub.shape[:2]

            def U(c, r):
                lon, lat = ref.lonlat(c, r)
                return to_utm(lat, lon, zone, north)
            items.append(make_item(np_to_qimage(sub), U(c0, r0), U(c0 + sw, r0), U(c0, r0 + sh)))
    return items


def image_center_latlon(arr, ref):
    h, w = arr.shape[:2]
    lon, lat = ref.lonlat(w / 2, h / 2)
    return lat, lon


# ---------------------------------------------------------------------------
# tile cache
# ---------------------------------------------------------------------------
class TileCache:
    def __init__(self, root, url_template):
        self.url = url_template
        self.dir = os.path.join(root, source_key(url_template))

    def path(self, z, x, y):
        return os.path.join(self.dir, str(z), str(x), f"{y}.img")

    def has(self, z, x, y):
        p = self.path(z, x, y)
        return os.path.exists(p) or os.path.exists(p + ".none")

    def levels(self):
        try:
            return sorted(int(d) for d in os.listdir(self.dir) if d.isdigit())
        except FileNotFoundError:
            return []

    def fetch(self, z, x, y, timeout=20):
        p = self.path(z, x, y)
        if self.has(z, x, y):
            return "cached"
        os.makedirs(os.path.dirname(p), exist_ok=True)
        url = self.url.format(z=z, x=x, y=y)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
        except urllib.error.HTTPError as e:
            if e.code in (404, 204):
                open(p + ".none", "w").close()     # remember "no imagery here"
                return "missing"
            raise
        tmp = p + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, p)
        return "ok"


def tiles_for_bbox(lat0, lon0, lat1, lon1, z):
    x0, y0 = lonlat_to_tile(min(lon0, lon1), max(lat0, lat1), z)
    x1, y1 = lonlat_to_tile(max(lon0, lon1), min(lat0, lat1), z)
    return [(z, x, y) for x in range(int(x0), int(x1) + 1) for y in range(int(y0), int(y1) + 1)]


def plan_download(lat0, lon0, lat1, lon1, zmax, levels_below=4):
    tiles = []
    for z in range(max(1, zmax - levels_below), zmax + 1):
        tiles += tiles_for_bbox(lat0, lon0, lat1, lon1, z)
    return tiles


class Downloader(threading.Thread):
    def __init__(self, cache: TileCache, tiles, workers=4):
        super().__init__(daemon=True)
        self.cache, self.tiles, self.workers = cache, tiles, workers
        self.done = self.failed = self.missing = 0
        self.cancelled = False
        self.error = None

    def run(self):
        def one(t):
            if self.cancelled:
                return
            try:
                r = self.cache.fetch(*t)
                if r == "missing":
                    self.missing += 1
            except Exception as e:      # network down, timeout ...
                self.failed += 1
                self.error = str(e)
            self.done += 1
        with ThreadPoolExecutor(self.workers) as ex:
            list(ex.map(one, self.tiles))


class TileLayer:
    """Shows cached tiles for the current view, choosing a sensible zoom level."""

    def __init__(self, viewbox, cache: TileCache):
        self.vb, self.cache = viewbox, cache
        self.items: dict[tuple, QtWidgets.QGraphicsPixmapItem] = {}
        self.zone = None
        self.north = True
        self.opacity = 1.0
        self.visible = True
        self.message = ""

    def set_cache(self, cache):
        self.clear()
        self.cache = cache

    def clear(self):
        for it in self.items.values():
            self.vb.removeItem(it)
        self.items.clear()

    def set_opacity(self, o):
        self.opacity = o
        for it in self.items.values():
            it.setOpacity(o)

    def update(self, px_width):
        if not self.visible or self.zone is None or self.cache is None:
            self.clear(); return
        levels = self.cache.levels()
        if not levels:
            self.clear(); self.message = ""; return
        (x0, x1), (y0, y1) = self.vb.viewRange()
        if x1 - x0 > 2e5:
            return
        lat0, lon0 = from_utm(x0, y0, self.zone, self.north)
        lat1, lon1 = from_utm(x1, y1, self.zone, self.north)
        latc = (lat0 + lat1) / 2
        view_res = (x1 - x0) / max(px_width, 1)
        ideal = int(math.floor(math.log2(156543.03392 * math.cos(math.radians(latc)) / max(view_res, 1e-3))))
        cands = [z for z in levels if z <= ideal] or [levels[0]]
        z = cands[-1]
        want = tiles_for_bbox(lat0, lon0, lat1, lon1, z)
        if len(want) > MAX_SHOWN_TILES:
            self.message = "zoom in to see imagery"
            want = []
        else:
            self.message = ""
        want = set(want)
        for k in list(self.items):
            if k not in want:
                self.vb.removeItem(self.items.pop(k))
        for t in want:
            if t in self.items:
                continue
            p = self.cache.path(*t)
            if not os.path.exists(p):
                continue
            q = QtGui.QImage(p)
            if q.isNull():
                continue
            zz, x, y = t
            def U(tx, ty):
                lon, lat = tile_to_lonlat(tx, ty, zz)
                return to_utm(lat, lon, self.zone, self.north)
            it = make_item(q, U(x, y), U(x + 1, y), U(x, y + 1))
            it.setOpacity(self.opacity)
            self.vb.addItem(it, ignoreBounds=True)
            self.items[t] = it


def estimate_mb(n_tiles):
    return n_tiles * 0.025      # ~25 kB per 256-px JPEG tile
