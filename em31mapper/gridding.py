"""Fast inverse-distance gridding for the live map, plus ESRI ASCII grid export."""
from __future__ import annotations

import numpy as np

from .geo import utm_prj_wkt

MAX_CELLS = 400_000      # keep the Mesa2 responsive


def grid_extent(e, n, cell, pad=1):
    """Lower-left corner and size of a grid covering the points; coarsens if huge."""
    while True:
        x0 = np.floor(np.min(e) / cell) * cell - pad * cell
        y0 = np.floor(np.min(n) / cell) * cell - pad * cell
        nx = int(np.ceil((np.max(e) - x0) / cell)) + 2 * pad
        ny = int(np.ceil((np.max(n) - y0) / cell)) + 2 * pad
        if nx * ny <= MAX_CELLS:
            return x0, y0, nx, ny, cell
        cell *= 1.5                      # keep the Mesa2 responsive on big surveys


def idw_grid(e, n, v, cell=1.0, radius=3.0, k=None, power=2.0, extent=None):
    """Inverse-distance-weighted grid of all points within `radius` of each cell centre.

    Returns (grid[ny, nx] with NaN outside coverage, x0, y0, cell), where x0, y0 are
    the lower-left corner of the lower-left cell. Pass `extent` (x0, y0, nx, ny, cell)
    to grid onto an existing frame. Pure NumPy: every point "splats" its weighted
    value onto the cells within the radius, so cost is O(points x cells-in-radius).
    (`k` is accepted for backwards compatibility and ignored.)
    """
    e, n, v = (np.asarray(a, float) for a in (e, n, v))
    ok = np.isfinite(e) & np.isfinite(n) & np.isfinite(v)
    e, n, v = e[ok], n[ok], v[ok]
    if len(v) < 3:
        return None
    x0, y0, nx, ny, cell = extent if extent is not None else grid_extent(e, n, cell)
    w0 = np.ones_like(v)

    r_cells = int(np.ceil(radius / cell)) + 1
    oy, ox = np.mgrid[-r_cells:r_cells + 1, -r_cells:r_cells + 1]
    keep = (np.hypot(ox, oy) - 1.5) * cell <= radius     # offsets that can reach the radius
    ox, oy = ox[keep], oy[keep]

    # Large surveys / small cells: pre-average points per cell to bound the cost.
    if len(v) * len(ox) > 2.5e7:
        ci = np.floor((e - x0) / cell).astype(np.int64)
        cj = np.floor((n - y0) / cell).astype(np.int64)
        key = cj * nx + ci
        uk, inv, cnt = np.unique(key, return_inverse=True, return_counts=True)
        e = np.bincount(inv, e) / cnt
        n = np.bincount(inv, n) / cnt
        v = np.bincount(inv, v) / cnt
        w0 = cnt.astype(float)

    fi = (e - x0) / cell - 0.5            # fractional cell index of each point
    fj = (n - y0) / cell - 0.5
    bi, bj = np.round(fi).astype(np.int64), np.round(fj).astype(np.int64)
    size = nx * ny
    num = np.zeros(size)
    den = np.zeros(size)
    eps = cell * 0.05
    for dx, dy in zip(ox, oy):
        gi, gj = bi + dx, bj + dy
        d = np.hypot((gi - fi) * cell, (gj - fj) * cell)
        m = (d <= radius) & (gi >= 0) & (gi < nx) & (gj >= 0) & (gj < ny)
        if not m.any():
            continue
        w = w0[m] / np.maximum(d[m], eps) ** power
        idx = gj[m] * nx + gi[m]
        num += np.bincount(idx, w * v[m], minlength=size)
        den += np.bincount(idx, w, minlength=size)
    with np.errstate(invalid="ignore", divide="ignore"):
        grid = np.where(den > 0, num / den, np.nan).reshape(ny, nx).astype(np.float32)
    return grid, x0, y0, cell


def diff_grid(ev, nv, vv, eh, nh, vh, cell=1.0, radius=3.0):
    """VMD minus HMD on a common grid; NaN wherever either mode lacks coverage."""
    ev, nv, vv, eh, nh, vh = map(np.asarray, (ev, nv, vv, eh, nh, vh))
    if np.isfinite(vv).sum() < 3 or np.isfinite(vh).sum() < 3:
        return None
    ext = grid_extent(np.concatenate([ev, eh]), np.concatenate([nv, nh]), cell)
    gv = idw_grid(ev, nv, vv, cell, radius, extent=ext)
    gh = idw_grid(eh, nh, vh, cell, radius, extent=ext)
    if gv is None or gh is None:
        return None
    d = gv[0] - gh[0]                    # NaN propagates where either is missing
    if not np.isfinite(d).any():
        return None
    return d.astype(np.float32), ext[0], ext[1], ext[4]


def write_ascii_grid(path, grid, x0, y0, cell, zone, north=True):
    ny, nx = grid.shape
    with open(path, "w") as f:
        f.write(f"ncols {nx}\nnrows {ny}\nxllcorner {x0:.3f}\nyllcorner {y0:.3f}\n"
                f"cellsize {cell:.4f}\nNODATA_value -9999\n")
        out = np.where(np.isfinite(grid), grid, -9999.0)[::-1]   # top row first
        np.savetxt(f, out, fmt="%.4f")
    with open(str(path)[:-4] + ".prj", "w") as f:
        f.write(utm_prj_wkt(zone, north))
