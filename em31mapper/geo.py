"""WGS84 <-> UTM (no external dependencies; ~mm accuracy within a zone)."""
from __future__ import annotations

import math

_A = 6378137.0
_F = 1 / 298.257223563
_E2 = _F * (2 - _F)
_EP2 = _E2 / (1 - _E2)
_K0 = 0.9996


def utm_zone(lat: float, lon: float) -> int:
    z = int((lon + 180) // 6) + 1
    if 56 <= lat < 64 and 3 <= lon < 12:
        return 32
    if 72 <= lat < 84:
        if 0 <= lon < 9: return 31
        if 9 <= lon < 21: return 33
        if 21 <= lon < 33: return 35
        if 33 <= lon < 42: return 37
    return z


def to_utm(lat: float, lon: float, zone: int, north: bool = True) -> tuple[float, float]:
    phi = math.radians(lat)
    lam = math.radians(lon)
    lam0 = math.radians((zone - 1) * 6 - 180 + 3)
    n = _A / math.sqrt(1 - _E2 * math.sin(phi) ** 2)
    t = math.tan(phi) ** 2
    c = _EP2 * math.cos(phi) ** 2
    a = math.cos(phi) * (lam - lam0)
    e4, e6 = _E2 ** 2, _E2 ** 3
    m = _A * ((1 - _E2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * phi
              - (3 * _E2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * math.sin(2 * phi)
              + (15 * e4 / 256 + 45 * e6 / 1024) * math.sin(4 * phi)
              - (35 * e6 / 3072) * math.sin(6 * phi))
    x = _K0 * n * (a + (1 - t + c) * a ** 3 / 6
                   + (5 - 18 * t + t * t + 72 * c - 58 * _EP2) * a ** 5 / 120) + 500000.0
    y = _K0 * (m + n * math.tan(phi) * (a * a / 2 + (5 - t + 9 * c + 4 * c * c) * a ** 4 / 24
                                        + (61 - 58 * t + t * t + 600 * c - 330 * _EP2) * a ** 6 / 720))
    if not north:
        y += 10000000.0
    return x, y


def utm_prj_wkt(zone: int, north: bool = True) -> str:
    """ESRI-style WKT for a .prj sidecar."""
    hemi = "N" if north else "S"
    cm = (zone - 1) * 6 - 180 + 3
    fn = 0 if north else 10000000
    return (
        f'PROJCS["WGS_1984_UTM_Zone_{zone}{hemi}",GEOGCS["GCS_WGS_1984",'
        'DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
        'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
        'PROJECTION["Transverse_Mercator"],PARAMETER["False_Easting",500000.0],'
        f'PARAMETER["False_Northing",{fn}],PARAMETER["Central_Meridian",{cm}],'
        'PARAMETER["Scale_Factor",0.9996],PARAMETER["Latitude_Of_Origin",0.0],'
        'UNIT["Meter",1.0]]')


def epsg_code(zone: int, north: bool = True) -> int:
    return (32600 if north else 32700) + zone


def from_utm(e: float, n: float, zone: int, north: bool = True) -> tuple[float, float]:
    """Inverse of to_utm -> (lat, lon) in degrees."""
    x = e - 500000.0
    y = n if north else n - 10000000.0
    e1 = (1 - math.sqrt(1 - _E2)) / (1 + math.sqrt(1 - _E2))
    m = y / _K0
    mu = m / (_A * (1 - _E2 / 4 - 3 * _E2 ** 2 / 64 - 5 * _E2 ** 3 / 256))
    phi1 = (mu + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
            + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
            + (151 * e1 ** 3 / 96) * math.sin(6 * mu)
            + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))
    s, c = math.sin(phi1), math.cos(phi1)
    n1 = _A / math.sqrt(1 - _E2 * s * s)
    t1 = math.tan(phi1) ** 2
    c1 = _EP2 * c * c
    r1 = _A * (1 - _E2) / (1 - _E2 * s * s) ** 1.5
    d = x / (n1 * _K0)
    lat = phi1 - (n1 * math.tan(phi1) / r1) * (
        d * d / 2 - (5 + 3 * t1 + 10 * c1 - 4 * c1 * c1 - 9 * _EP2) * d ** 4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1 * t1 - 252 * _EP2 - 3 * c1 * c1) * d ** 6 / 720)
    lon = (d - (1 + 2 * t1 + c1) * d ** 3 / 6
           + (5 - 2 * c1 + 28 * t1 - 3 * c1 * c1 + 8 * _EP2 + 24 * t1 * t1) * d ** 5 / 120) / c
    lon0 = (zone - 1) * 6 - 180 + 3
    return math.degrees(lat), lon0 + math.degrees(lon)


# --- Web Mercator / slippy-map tiles ------------------------------------------
_R = 6378137.0


def merc_to_lonlat(x: float, y: float) -> tuple[float, float]:
    lon = math.degrees(x / _R)
    lat = math.degrees(2 * math.atan(math.exp(y / _R)) - math.pi / 2)
    return lon, lat


def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[float, float]:
    n = 2 ** z
    lat_r = math.radians(max(-85.05, min(85.05, lat)))
    return (lon + 180) / 360 * n, (1 - math.asinh(math.tan(lat_r)) / math.pi) / 2 * n


def tile_to_lonlat(x: float, y: float, z: int) -> tuple[float, float]:
    n = 2 ** z
    lon = x / n * 360 - 180
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lon, lat


def tile_resolution(lat: float, z: int) -> float:
    """Ground metres per pixel of a 256-px tile."""
    return 156543.03392 * math.cos(math.radians(lat)) / 2 ** z


# --- CRS helpers for vector data ------------------------------------------------
GEOGRAPHIC_EPSG = {4326, 4269, 4258, 4283, 4617, 4979, 4167, 4019}
WEBMERC_EPSG = {3857, 900913, 3785, 102100, 102113}


def utm_from_epsg(code: int):
    """(zone, north) for WGS84 / NAD83 / ETRS89 UTM codes, else None."""
    if 32601 <= code <= 32660:
        return code - 32600, True
    if 32701 <= code <= 32760:
        return code - 32700, False
    if 26901 <= code <= 26923:          # NAD83 / UTM zone 1N-23N
        return code - 26900, True
    if 25828 <= code <= 25838:          # ETRS89 / UTM zone 28N-38N
        return code - 25800, True
    return None


def epsg_to_latlon(code: int):
    """Return f(x, y) -> (lat, lon) for a supported EPSG code, or raise ValueError.

    NAD83 and ETRS89 are treated as WGS84 (they differ by about 1 m or less).
    """
    if code in GEOGRAPHIC_EPSG:
        return lambda x, y: (y, x)
    if code in WEBMERC_EPSG:
        return lambda x, y: merc_to_lonlat(x, y)[::-1]
    utm = utm_from_epsg(code)
    if utm:
        z, north = utm
        return lambda x, y: from_utm(x, y, z, north)
    raise ValueError(f"EPSG:{code} isn't supported. Save the file in WGS84 lat/lon "
                     "(EPSG:4326) or WGS84 UTM (EPSG:326xx) and load it again.")


def epsg_from_name(name: str):
    """Parse 'EPSG:32611', 'urn:ogc:def:crs:EPSG::32611', 'CRS84', ... -> int or None."""
    import re
    s = name.strip().upper()
    if s.endswith("CRS84") or s.endswith("CRS:84"):
        return 4326
    m = re.search(r"EPSG:+(?:[\d.]*:)?(\d{4,6})$", s) or re.search(r"(\d{4,6})$", s)
    return int(m.group(1)) if m else None
