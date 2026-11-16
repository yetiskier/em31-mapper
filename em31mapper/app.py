"""EM31 Mapper — real-time EM31-MK2 + GPS conductivity mapping (Windows / Mesa2)."""
from __future__ import annotations

import concurrent.futures as cf
import csv
import json
import math
import os
import queue
import sys
import time
import traceback
from collections import deque

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from . import __version__
from .em31 import EM31Parser
from .fusion import Fuser
from .geo import from_utm, tile_resolution, to_utm
from . import imagery as im
from .mapnav import MapNavigator
from .plan import (FILE_FILTER, Plan, PlanError, default_selection, load_plan, plan_from_layers,
                   read_layers)
from .gridding import diff_grid, idw_grid, write_ascii_grid
from .logger import SurveyLogger
from .nmea import NMEAReader, QUALITY_NAMES
from .sources import SerialSource, SimSource, SimWalker, list_ports

ORG, APP = "EM31Mapper", "EM31Mapper"
MAX_SCATTER = 25_000          # points drawn individually; the grid shows everything
PROFILE_SECONDS = 60

DEFAULTS = {
    "em_port": "", "gps_port": "", "em_baud": 9600, "gps_baud": 9600,
    "simulate": False, "component": "both", "short_boom": False,
    "gps_latency": 0.10, "em_latency": 0.0, "layback": 0.0, "cross_offset": 0.0,
    "max_gap": 3.0, "min_quality": 1,
    "out_folder": os.path.join(os.path.expanduser("~"), "Documents", "EM31Mapper"),
    "channel": "cond", "cmap": "viridis", "auto_range": True, "vmin": 0.0, "vmax": 100.0,
    "cell": 1.0, "radius": 3.0, "grid_interval": 3.0, "show_grid": True, "show_points": True,
    "follow": True, "grid_opacity": 0.75,
    "plan_path": "", "plan_sources": "", "image_path": "", "image_crs": "",
    "tiles_on": True, "tile_source": next(iter(im.TILE_SOURCES)), "tile_url": "",
    "tile_zoom": 18, "imagery_opacity": 1.0, "line_mode": "manual", "line_tol": 0.5,
    "dipole_expected": "VMD", "map_dipole": "auto", "depth_vmd": 6.0, "depth_hmd": 3.0,
}


DIFF = "diff"
DIFF_CMAP = "CET-D1"          # diverging: blue = HMD higher, red = VMD higher


def mode_name(mode) -> str:
    if mode == DIFF:
        return "VMD−HMD"
    return "VMD" if mode else "HMD"
CUSTOM_SRC = "Custom URL template"
CMAPS = ["viridis", "plasma", "inferno", "magma", "cividis", "turbo", "CET-R4", "CET-D1"]


# ---------------------------------------------------------------------------
class Settings:
    def __init__(self):
        self.q = QtCore.QSettings(globals()["ORG"], APP)

    def get(self, key):
        d = DEFAULTS[key]
        v = self.q.value(key, d)
        if isinstance(d, bool):
            return v in (True, "true", "1", 1)
        if isinstance(d, int):
            return int(v)
        if isinstance(d, float):
            return float(v)
        return str(v)

    def set(self, key, value):
        self.q.setValue(key, value)


_ACCEPT = {1: {1, 2, 3, 4, 5, 8, 9}, 2: {2, 3, 4, 5, 9}, 5: {4, 5}, 4: {4}}


def quality_ok(q: int, minimum: int) -> bool:
    """GGA quality codes aren't ordered by accuracy (4=RTK fixed beats 5=float)."""
    return q in _ACCEPT.get(minimum, _ACCEPT[1])


class PlainAxis(pg.AxisItem):
    """UTM coordinates as plain metres instead of 4.98e+06."""

    def tickStrings(self, values, scale, spacing):
        fmt = "{:.0f}" if spacing >= 1 else "{:.1f}"
        return [fmt.format(v * scale) for v in values]


def big_button(text, color=None, checkable=False, checked_color="#b3261e"):
    b = QtWidgets.QPushButton(text)
    b.setMinimumHeight(56)
    b.setCheckable(checkable)
    b.setFocusPolicy(QtCore.Qt.NoFocus)
    if color:
        b.setStyleSheet(
            "QPushButton{background:%s;color:white;font-weight:600;border-radius:6px;padding:6px}"
            "QPushButton:checked{background:%s}"
            "QPushButton:disabled{background:#9e9e9e}" % (color, checked_color))
    return b


# ---------------------------------------------------------------------------
class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, s: Settings, parent=None):
        super().__init__(parent)
        self.s = s
        self.setWindowTitle("Settings")
        self.resize(560, 640)
        tabs = QtWidgets.QTabWidget()
        self.w = {}

        def form():
            page = QtWidgets.QWidget()
            f = QtWidgets.QFormLayout(page)
            f.setRowWrapPolicy(QtWidgets.QFormLayout.WrapLongRows)
            f.setVerticalSpacing(12)
            sa = QtWidgets.QScrollArea(); sa.setWidgetResizable(True); sa.setWidget(page)
            QtWidgets.QScroller.grabGesture(sa.viewport(), QtWidgets.QScroller.LeftMouseButtonGesture)
            return sa, f

        def combo(key, items, editable=False):
            c = QtWidgets.QComboBox(); c.setEditable(editable); c.setMinimumHeight(40)
            for data, label in items:
                c.addItem(label, data)
            i = c.findData(s.get(key))
            if i >= 0:
                c.setCurrentIndex(i)
            elif editable:
                c.setEditText(str(s.get(key)))
            self.w[key] = c
            return c

        def dspin(key, lo, hi, step, dec, suffix=""):
            d = QtWidgets.QDoubleSpinBox(); d.setRange(lo, hi); d.setSingleStep(step)
            d.setDecimals(dec); d.setValue(s.get(key)); d.setSuffix(suffix); d.setMinimumHeight(40)
            self.w[key] = d
            return d

        def check(key, text):
            c = QtWidgets.QCheckBox(text); c.setChecked(s.get(key)); self.w[key] = c
            return c

        # --- Ports
        sa, f = form()
        ports = [("", "(none)")] + list_ports()
        f.addRow("EM31 port", combo("em_port", ports, editable=True))
        f.addRow("EM31 baud", combo("em_baud", [(b, str(b)) for b in (9600, 19200, 38400)]))
        f.addRow("GPS port", combo("gps_port", ports, editable=True))
        f.addRow("GPS baud", combo("gps_baud", [(b, str(b)) for b in
                                                (4800, 9600, 19200, 38400, 57600, 115200)]))
        f.addRow(check("simulate", "Simulate instruments (no hardware)"))
        note = QtWidgets.QLabel(
            "EM31-MK2 runs at 9600 8N1. Bluetooth links appear as outgoing COM ports "
            "(Bluetooth settings → More Bluetooth options → COM Ports). Close EM31win "
            "and any GPS utility that holds the port open.")
        note.setWordWrap(True); note.setStyleSheet("color:#666")
        f.addRow(note)
        tabs.addTab(sa, "Ports")

        # --- Instrument
        sa, f = form()
        f.addRow("Component", combo("component", [("both", "Conductivity + In-phase (OPER)"),
                                                  ("inphase", "In-phase only (COMP)")]))
        f.addRow(check("short_boom", "EM31-SH short boom (in-phase ÷ 3.35)"))
        f.addRow("Survey dipole mode", combo("dipole_expected", [
            ("VMD", "VMD – vertical dipole (deeper, ~6 m)"),
            ("HMD", "HMD – horizontal dipole (shallower, ~3 m)")]))
        f.addRow("VMD nominal depth", dspin("depth_vmd", 0.5, 20, 0.5, 1, " m"))
        f.addRow("HMD nominal depth", dspin("depth_hmd", 0.25, 10, 0.25, 2, " m"))
        note = QtWidgets.QLabel(
            "The dipole mode is set on the instrument: normal carry = VMD; rotate the EM31 "
            "90° about the boom axis for HMD. Every record reports the actual mode, so each "
            "reading is tagged and mapped by the mode it was really taken in, and you're "
            "warned if it doesn't match the mode chosen here. Depths are nominal effective "
            "exploration depths (≈1.5× and 0.75× coil spacing); for the 2 m EM31-SH use "
            "about 3 m / 1.5 m.")
        note.setWordWrap(True); note.setStyleSheet("color:#666")
        f.addRow(note)
        tabs.addTab(sa, "Instrument")

        # --- Positioning
        sa, f = form()
        f.addRow("GPS latency", dspin("gps_latency", 0, 2, 0.05, 2, " s"))
        f.addRow("EM31 latency", dspin("em_latency", 0, 2, 0.05, 2, " s"))
        f.addRow("Layback (sensor behind antenna)", dspin("layback", -20, 20, 0.1, 2, " m"))
        f.addRow("Cross-track (sensor right of antenna)", dspin("cross_offset", -20, 20, 0.1, 2, " m"))
        f.addRow("Max GPS gap to interpolate", dspin("max_gap", 0.2, 30, 0.5, 1, " s"))
        f.addRow("Minimum fix quality", combo("min_quality", [
            (1, "1 – autonomous GPS"), (2, "2 – DGPS / SBAS"), (5, "5 – RTK float"), (4, "4 – RTK fixed only")]))
        tabs.addTab(sa, "Positioning")

        # --- Map
        sa, f = form()
        f.addRow("Map channel", combo("channel", [("cond", "Conductivity (mS/m)"),
                                                  ("inphase", "In-phase (ppt)")]))
        f.addRow("Map dipole mode", combo("map_dipole", [
            ("auto", "Follow the instrument's current mode"),
            ("VMD", "VMD readings (~6 m)"), ("HMD", "HMD readings (~3 m)"),
            (DIFF, "Difference VMD − HMD")]))
        f.addRow("Colour map", combo("cmap", [(c, c) for c in CMAPS]))
        f.addRow(check("auto_range", "Auto colour range (2–98 percentile)"))
        f.addRow("Fixed min", dspin("vmin", -1000, 1000, 1, 2))
        f.addRow("Fixed max", dspin("vmax", -1000, 1000, 1, 2))
        f.addRow("Grid cell size", dspin("cell", 0.1, 50, 0.25, 2, " m"))
        f.addRow("Grid search radius", dspin("radius", 0.2, 100, 0.5, 1, " m"))
        f.addRow("Regrid every", dspin("grid_interval", 1, 60, 1, 0, " s"))
        f.addRow(check("show_grid", "Show interpolated grid"))
        f.addRow(check("show_points", "Show measurement points"))
        f.addRow("Grid / point opacity", dspin("grid_opacity", 0.1, 1, 0.05, 2))
        tabs.addTab(sa, "Map")

        # --- Layers
        sa, f = form()
        srcs = [(k, k) for k in im.TILE_SOURCES] + [(CUSTOM_SRC, CUSTOM_SRC)]
        f.addRow("Tile imagery source", combo("tile_source", srcs))
        self.tile_url = QtWidgets.QLineEdit(s.get("tile_url")); self.tile_url.setMinimumHeight(40)
        self.tile_url.setPlaceholderText("https://server/path/{z}/{x}/{y}.jpg  (or …/{z}/{y}/{x})")
        f.addRow("Custom URL", self.tile_url)
        f.addRow(check("tiles_on", "Show downloaded imagery"))
        f.addRow("Imagery opacity", dspin("imagery_opacity", 0.1, 1, 0.05, 2))
        f.addRow("Line selection", combo("line_mode", [("manual", "Manual (New line = next planned line)"),
                                                       ("nearest", "Automatic: nearest line")]))
        f.addRow("On-line tolerance", dspin("line_tol", 0.05, 10, 0.05, 2, " m"))
        note = QtWidgets.QLabel(
            "Custom sources: you're responsible for the provider's terms. Many commercial "
            "basemaps (Google, Bing, Esri World Imagery) don't permit bulk offline caching. "
            "USGS imagery is public domain.")
        note.setWordWrap(True); note.setStyleSheet("color:#666")
        f.addRow(note)
        tabs.addTab(sa, "Layers")

        # --- Data
        sa, f = form()
        row = QtWidgets.QHBoxLayout()
        self.folder = QtWidgets.QLineEdit(s.get("out_folder")); self.folder.setMinimumHeight(40)
        pick = QtWidgets.QPushButton("…"); pick.setMinimumHeight(40)
        pick.clicked.connect(self._pick_folder)
        row.addWidget(self.folder); row.addWidget(pick)
        f.addRow("Data folder", row)
        tabs.addTab(sa, "Data")

        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        for b in bb.buttons():
            b.setMinimumHeight(48)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        lay = QtWidgets.QVBoxLayout(self); lay.addWidget(tabs); lay.addWidget(bb)

    def _pick_folder(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Data folder", self.folder.text())
        if d:
            self.folder.setText(d)

    def save(self):
        for k, w in self.w.items():
            if isinstance(w, QtWidgets.QComboBox):
                v = w.currentData()
                if w.isEditable() and (v is None or w.currentText() != w.itemText(w.currentIndex())):
                    v = w.currentText().split(" ")[0]
                self.s.set(k, v if v is not None else "")
            elif isinstance(w, QtWidgets.QDoubleSpinBox):
                self.s.set(k, w.value())
            elif isinstance(w, QtWidgets.QCheckBox):
                self.s.set(k, w.isChecked())
        self.s.set("out_folder", self.folder.text())
        self.s.set("tile_url", self.tile_url.text().strip())


class MonitorDialog(QtWidgets.QDialog):
    """Live view of the raw serial streams, for checking cables and formats."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Raw stream monitor")
        self.resize(600, 520)
        self.em = QtWidgets.QPlainTextEdit(); self.em.setReadOnly(True)
        self.gps = QtWidgets.QPlainTextEdit(); self.gps.setReadOnly(True)
        for t in (self.em, self.gps):
            t.setMaximumBlockCount(300)
            t.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(QtWidgets.QLabel("EM31 (hex | ascii)")); lay.addWidget(self.em)
        lay.addWidget(QtWidgets.QLabel("GPS (NMEA)")); lay.addWidget(self.gps)
        self._em_buf = b""

    def add_em(self, data: bytes):
        self._em_buf += data
        while len(self._em_buf) >= 22:
            chunk, self._em_buf = self._em_buf[:22], self._em_buf[22:]
            hx = " ".join(f"{b:02X}" for b in chunk)
            asc = "".join(chr(b) if 32 <= b < 127 else "·" for b in chunk)
            self.em.appendPlainText(f"{hx:<66} {asc}")

    def add_gps(self, line: str):
        self.gps.appendPlainText(line)


class LayerDialog(QtWidgets.QDialog):
    """Pick which layers of a multi-layer file (e.g. a GeoPackage) to show."""

    def __init__(self, path, layers, selected, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose layers")
        self.resize(520, 120 + 56 * len(layers))
        v = QtWidgets.QVBoxLayout(self)
        t = QtWidgets.QLabel(f"<b>{os.path.basename(path)}</b> has {len(layers)} layers. "
                             "Lines are followed with guidance; boundaries are drawn as outlines.")
        t.setWordWrap(True)
        v.addWidget(t)
        self.boxes = []
        for lay, on in zip(layers, selected):
            cb = QtWidgets.QCheckBox(f"{lay.name}  —  {lay.describe()}")
            cb.setChecked(on); cb.setMinimumHeight(44)
            cb.setStyleSheet("QCheckBox{font-size:15px} QCheckBox::indicator{width:28px;height:28px}")
            v.addWidget(cb); self.boxes.append((lay, cb))
        hint = QtWidgets.QLabel("Tip: a serpentine track and transect lines show the same route; "
                                "pick transects for line-by-line guidance, or the track to follow one "
                                "continuous path with turn countdowns.")
        hint.setWordWrap(True); hint.setStyleSheet("color:#666")
        v.addWidget(hint)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        for b in bb.buttons():
            b.setMinimumHeight(48)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def selected(self):
        return [lay for lay, cb in self.boxes if cb.isChecked()]


class DownloadDialog(QtWidgets.QDialog):
    """Choose an area + zoom; shows tile count and size before downloading."""

    def __init__(self, mw: "MainWindow"):
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle("Download imagery for offline use")
        self.resize(520, 480)
        self.tiles, self.center, self.fit_rect = [], None, None
        f = QtWidgets.QFormLayout(self); f.setVerticalSpacing(12)
        src = mw.s.get("tile_source")
        f.addRow("Source", QtWidgets.QLabel(src if src != CUSTOM_SRC else mw.s.get("tile_url")))
        self.area = QtWidgets.QComboBox(); self.area.setMinimumHeight(40)
        if mw.plan is not None:
            self.area.addItem("Planned lines + margin", "plan")
        if mw.zone is not None:
            self.area.addItem("Current map view", "view")
        self.area.addItem("Around a point (lat/lon + radius)", "point")
        f.addRow("Area", self.area)
        self.margin = QtWidgets.QDoubleSpinBox(); self.margin.setRange(0, 5000); self.margin.setValue(100)
        self.margin.setSuffix(" m"); self.margin.setMinimumHeight(40)
        f.addRow("Margin / radius", self.margin)
        lat0, lon0 = "", ""
        if mw.last_fix is not None:
            lat0, lon0 = f"{mw.last_fix.lat:.6f}", f"{mw.last_fix.lon:.6f}"
        self.lat = QtWidgets.QLineEdit(lat0); self.lon = QtWidgets.QLineEdit(lon0)
        for w in (self.lat, self.lon):
            w.setMinimumHeight(40)
        self.lat.setPlaceholderText("e.g. 45.12345"); self.lon.setPlaceholderText("e.g. -110.12345")
        f.addRow("Centre latitude", self.lat); f.addRow("Centre longitude", self.lon)
        self.zoom = QtWidgets.QSpinBox(); self.zoom.setRange(12, 21); self.zoom.setValue(mw.s.get("tile_zoom"))
        self.zoom.setMinimumHeight(40)
        f.addRow("Max detail (zoom)", self.zoom)
        self.info = QtWidgets.QLabel(); self.info.setWordWrap(True)
        f.addRow(self.info)
        hint = QtWidgets.QLabel("Zoom 18 ≈ 0.4–0.6 m/pixel (native NAIP resolution); 19 doubles "
                                "detail but quadruples tiles. Four coarser levels are fetched too, "
                                "for zoomed-out views. Do this before you leave signal.")
        hint.setWordWrap(True); hint.setStyleSheet("color:#666")
        f.addRow(hint)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.button(QtWidgets.QDialogButtonBox.Ok).setText("Download")
        for b in bb.buttons():
            b.setMinimumHeight(48)
        bb.accepted.connect(self._accept); bb.rejected.connect(self.reject)
        self.ok_btn = bb.button(QtWidgets.QDialogButtonBox.Ok)
        f.addRow(bb)
        for w in (self.area, ):
            w.currentIndexChanged.connect(self._recalc)
        for w in (self.margin, self.zoom):
            w.valueChanged.connect(self._recalc)
        for w in (self.lat, self.lon):
            w.textChanged.connect(self._recalc)
        self._recalc()

    def _bbox(self):
        """Return (lat0, lon0, lat1, lon1, centre, utm view rect) or raise ValueError."""
        mw, kind, m = self.mw, self.area.currentData(), self.margin.value()
        point = kind == "point"
        self.lat.setEnabled(point); self.lon.setEnabled(point)
        if kind == "point":
            la, lo = float(self.lat.text()), float(self.lon.text())
            dla = m / 111132.0; dlo = m / (111320.0 * max(0.05, math.cos(math.radians(la))))
            return la - dla, lo - dlo, la + dla, lo + dlo, (la, lo), None
        if kind == "plan":
            e0, e1, n0, n1 = mw.plan.bounds()
        else:
            (e0, e1), (n0, n1) = mw.map.getViewBox().viewRange()
            m = 0
        e0, e1, n0, n1 = e0 - m, e1 + m, n0 - m, n1 + m
        a = from_utm(e0, n0, mw.zone, mw.fuser.north_hemi)
        b = from_utm(e1, n1, mw.zone, mw.fuser.north_hemi)
        return a[0], a[1], b[0], b[1], None, ((e0, e1), (n0, n1))

    def _recalc(self):
        try:
            la0, lo0, la1, lo1, c, rect = self._bbox()
        except (ValueError, TypeError):
            self.tiles = []; self.info.setText("Enter a centre latitude and longitude.")
            self.ok_btn.setEnabled(False); return
        z = self.zoom.value()
        self.tiles = im.plan_download(la0, lo0, la1, lo1, z)
        self.center, self.fit_rect = c, rect
        cache = self.mw.tile_layer.cache
        have = sum(cache.has(*t) for t in self.tiles) if len(self.tiles) < 30000 else 0
        res = tile_resolution((la0 + la1) / 2, z)
        too_many = len(self.tiles) > im.MAX_DOWNLOAD_TILES
        self.info.setText(
            f"<b>{len(self.tiles):,} tiles</b> (~{im.estimate_mb(len(self.tiles) - have):.0f} MB to fetch, "
            f"{have:,} already cached) · {res:.2f} m/pixel"
            + ("<br><span style='color:#c62828'>Too many tiles — shrink the area or lower the zoom.</span>"
               if too_many else ""))
        self.ok_btn.setEnabled(bool(self.tiles) and not too_many)

    def _accept(self):
        self.mw.s.set("tile_zoom", self.zoom.value())
        self.accept()


# ---------------------------------------------------------------------------
class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"EM31 Mapper {__version__}")
        self.s = Settings()
        self.q: queue.Queue = queue.Queue()
        self.sources = []
        self.logger: SurveyLogger | None = None
        self.line_no = 1
        self.mark_no = 0
        self.monitor: MonitorDialog | None = None
        self.pool = cf.ThreadPoolExecutor(max_workers=1)
        self.grid_future = None
        self.last_grid = None

        # map data (growable lists → numpy on redraw)
        self.pe, self.pn, self.pc, self.pi = [], [], [], []
        self.pv = []                     # True = VMD, False = HMD, per mapped point
        self._shown_mode = None
        self.marks_e, self.marks_n = [], []
        self.track = deque(maxlen=3000)
        self.prof_t = deque(); self.prof_c = deque(); self.prof_i = deque()
        self.last_em = None; self.last_em_t = 0.0
        self.last_fix = None; self.last_fix_t = 0.0
        self.em_times = deque(maxlen=50)
        self.fuser = Fuser()
        self.zone = None
        self.plan: Plan | None = None
        self.active_line: int | None = None
        self.plan_items = []
        self.file_items = []
        self.tile_layer = None
        self.cur_pos = None            # current sensor position (E, N)
        self._dirty = False
        self._levels = None

        self.t_drain = QtCore.QTimer(self, interval=40, timeout=self._drain)
        self.t_draw = QtCore.QTimer(self, interval=500, timeout=self._redraw)
        self.t_grid = QtCore.QTimer(self, timeout=self._regrid)
        self.t_status = QtCore.QTimer(self, interval=500, timeout=self._update_status)

        self.t_tiles = QtCore.QTimer(self, singleShot=True, interval=250, timeout=self._update_tiles)

        self._build_ui()
        self._apply_settings()
        for t in (self.t_drain, self.t_draw, self.t_status):
            t.start()
        QtCore.QTimer.singleShot(0, self._restore_layers)

    # zone is owned by the fuser so GPS, plan and imagery always agree
    @property
    def zone(self):
        return self.fuser.zone

    @zone.setter
    def zone(self, z):
        self.fuser.zone = z

    # -- UI construction ---------------------------------------------------
    def _build_ui(self):
        pg.setConfigOptions(antialias=False, background="w", foreground="#222")
        central = QtWidgets.QWidget(); self.setCentralWidget(central)
        self.main_box = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.TopToBottom, central)
        self.main_box.setContentsMargins(4, 4, 4, 4)

        # map + profile
        plots = QtWidgets.QSplitter(QtCore.Qt.Vertical)
        map_box = QtWidgets.QWidget()
        mv = QtWidgets.QVBoxLayout(map_box); mv.setContentsMargins(0, 0, 0, 0); mv.setSpacing(2)
        self.banner = QtWidgets.QLabel(); self.banner.setAlignment(QtCore.Qt.AlignCenter)
        self.banner.setMinimumHeight(48); self.banner.hide()
        mv.addWidget(self.banner)
        self.map = pg.PlotWidget(axisItems={"bottom": PlainAxis("bottom"), "left": PlainAxis("left")})
        self.map.setAspectLocked(True)
        self.map.showGrid(x=True, y=True, alpha=0.2)
        self.map.setLabel("bottom", "Easting", "m"); self.map.setLabel("left", "Northing", "m")
        for ax in ("bottom", "left"):
            self.map.getAxis(ax).enableAutoSIPrefix(False)
        self.img = pg.ImageItem(); self.img.setZValue(-10)
        self.map.addItem(self.img)
        self.scatter = pg.ScatterPlotItem(size=5, pen=None, pxMode=True)
        self.map.addItem(self.scatter)
        self.track_curve = pg.PlotCurveItem(pen=pg.mkPen("#333", width=1.5), antialias=True)
        self.map.addItem(self.track_curve)
        self.marks = pg.ScatterPlotItem(symbol="t", size=14, brush="#e53935", pen="w")
        self.map.addItem(self.marks)
        self.here = pg.ScatterPlotItem(symbol="o", size=18, brush=pg.mkBrush(255, 0, 0, 200),
                                       pen=pg.mkPen("w", width=2))
        self.map.addItem(self.here)
        self.cbar = pg.ColorBarItem(values=(0, 100), width=14, interactive=False)
        self.cbar.setImageItem(self.img, insert_in=self.map.getPlotItem())
        self.plan_curve = pg.PlotCurveItem(pen=pg.mkPen("#00e5ff", width=2, style=QtCore.Qt.DashLine),
                                           connect="finite", antialias=True)
        self.plan_active = pg.PlotCurveItem(pen=pg.mkPen("#ff00ff", width=4), antialias=True)
        self.plan_wpts = pg.ScatterPlotItem(symbol="s", size=10, brush="#ffeb3b", pen="k")
        self.plan_bounds = pg.PlotCurveItem(pen=pg.mkPen("#ff9100", width=3), connect="finite",
                                            antialias=True)
        for it in (self.plan_bounds, self.plan_curve, self.plan_active, self.plan_wpts):
            it.setZValue(5); self.map.addItem(it)
        self.plan_bounds.setZValue(4)
        self.track_curve.setZValue(6); self.marks.setZValue(7); self.here.setZValue(8)
        self.tile_msg = pg.TextItem("", color="#c62828", anchor=(0.5, 0))
        self.map.setMenuEnabled(False)          # press-and-hold on a touchscreen = right click
        self.map.scene().sigMouseClicked.connect(self._map_clicked)
        self.map.getViewBox().sigRangeChanged.connect(lambda *_: self.t_tiles.start())
        self.map.getViewBox().sigRangeChangedManually.connect(lambda *_: self._set_follow(False))
        self.nav = MapNavigator(self.map, on_tap=self._tap_select,
                                on_user_nav=lambda: self._set_follow(False),
                                zoom_center=self._zoom_center)
        mv.addWidget(self.map, 1)
        plots.addWidget(map_box)

        self.prof = pg.GraphicsLayoutWidget()
        self.p_c = self.prof.addPlot(row=0, col=0)
        self.p_i = self.prof.addPlot(row=1, col=0)
        self.p_i.setXLink(self.p_c)
        self.p_c.setLabel("left", "σa", "mS/m"); self.p_i.setLabel("left", "In-ph", "ppt")
        self.p_i.setLabel("bottom", "seconds ago")
        for p in (self.p_c, self.p_i):
            p.showGrid(y=True, alpha=0.2); p.setMouseEnabled(x=False, y=False)
            p.getAxis("left").setWidth(52); p.getAxis("left").enableAutoSIPrefix(False)
        self.c_c = self.p_c.plot(pen=pg.mkPen("#1565c0", width=2))
        self.c_i = self.p_i.plot(pen=pg.mkPen("#ef6c00", width=2))
        self.prof.setMinimumHeight(170)
        plots.addWidget(self.prof)
        plots.setSizes([700, 180])
        self.main_box.addWidget(plots, 1)

        # control panel: readouts + buttons
        panel = QtWidgets.QWidget()
        self.panel_box = QtWidgets.QBoxLayout(QtWidgets.QBoxLayout.TopToBottom, panel)
        self.panel_box.setContentsMargins(0, 0, 0, 0)

        ro = QtWidgets.QGridLayout(); ro.setHorizontalSpacing(10)

        def readout(title):
            box = QtWidgets.QFrame(); box.setFrameShape(QtWidgets.QFrame.StyledPanel)
            v = QtWidgets.QVBoxLayout(box); v.setContentsMargins(6, 2, 6, 2); v.setSpacing(0)
            t = QtWidgets.QLabel(title); t.setStyleSheet("color:#666;font-size:11px")
            val = QtWidgets.QLabel("—"); val.setStyleSheet("font-size:24px;font-weight:600")
            sub = QtWidgets.QLabel(""); sub.setStyleSheet("color:#444;font-size:11px")
            v.addWidget(t); v.addWidget(val); v.addWidget(sub)
            return box, val, sub

        b1, self.v_cond, self.s_cond = readout("Conductivity (mS/m)")
        b2, self.v_inph, self.s_inph = readout("In-phase (ppt)")
        b3, self.v_gps, self.s_gps = readout("GPS")
        b4, self.v_log, self.s_log = readout("Recording")
        self.readouts = [b1, b2, b3, b4]
        self.ro_grid = ro
        self.panel_box.addLayout(ro)

        self.btn_connect = big_button("▶ Connect", "#2e7d32", checkable=True)
        self.btn_record = big_button("● Record", "#c62828", checkable=True)
        self.btn_mark = big_button("⚑ Mark", "#6a1b9a")
        self.btn_line = big_button("New line", "#37474f")
        self.btn_fit = big_button("⤢ Fit", "#455a64")
        self.btn_follow = big_button("⌖ Follow", "#455a64", checkable=True, checked_color="#1565c0")
        self.btn_settings = big_button("⚙ Settings", "#455a64")
        self.btn_more = big_button("☰ More", "#455a64")
        self.buttons = [self.btn_connect, self.btn_record, self.btn_mark, self.btn_line,
                        self.btn_fit, self.btn_follow, self.btn_settings, self.btn_more]
        self.btn_grid = QtWidgets.QGridLayout()
        self.panel_box.addLayout(self.btn_grid)
        self.main_box.addWidget(panel)

        menu = QtWidgets.QMenu(self)
        menu.addAction("Load planned lines / boundary (GeoPackage, GeoJSON, CSV, GPX, KML)…",
                       self._load_plan_dialog)
        menu.addAction("Previous planned line", lambda: self._step_line(-1))
        menu.addAction("Clear planned lines", self._clear_plan)
        menu.addSeparator()
        menu.addAction("Download imagery for offline use…", self._download_dialog)
        menu.addAction("Load imagery file (GeoTIFF / image + world file)…", self._load_image_dialog)
        menu.addAction("Remove imagery file", self._clear_image)
        menu.addSeparator()
        mm = menu.addMenu("Map dipole mode")
        self.mode_actions = {}
        grp = QtGui.QActionGroup(self)
        for key, label in (("auto", "Follow instrument"), ("VMD", "VMD (~6 m)"), ("HMD", "HMD (~3 m)"),
                           (DIFF, "Difference VMD − HMD")):
            a = mm.addAction(label); a.setCheckable(True); grp.addAction(a)
            a.triggered.connect(lambda _=False, k=key: self._set_map_dipole(k))
            self.mode_actions[key] = a
        menu.addSeparator()
        menu.addAction("Raw stream monitor", self._show_monitor)
        menu.addAction("Load survey CSV onto map…", self._load_csv)
        menu.addAction("Export grid (ESRI ASCII .asc)…", self._export_grid)
        menu.addAction("Export map image (PNG)…", self._export_png)
        menu.addSeparator()
        menu.addAction("Clear map", self._clear_map)
        menu.addAction("Open data folder", lambda: QtGui.QDesktopServices.openUrl(
            QtCore.QUrl.fromLocalFile(self.s.get("out_folder"))))
        menu.addSeparator()
        menu.addAction("Quit", self.close)
        self.btn_more.setMenu(menu)

        self.btn_connect.toggled.connect(self._toggle_connect)
        self.btn_record.toggled.connect(self._toggle_record)
        self.btn_mark.clicked.connect(self._mark)
        self.btn_line.clicked.connect(self._new_line)
        self.btn_fit.clicked.connect(self._fit)
        self.btn_follow.toggled.connect(lambda on: self._set_follow(on, save=True))
        self.btn_settings.clicked.connect(self._settings)
        self.btn_record.setEnabled(False)

        self.status = QtWidgets.QLabel(); self.statusBar().addWidget(self.status, 1)
        self._relayout(force=True)

    def _relayout(self, force=False):
        portrait = self.height() >= self.width()
        if not force and getattr(self, "_portrait", None) == portrait:
            return
        self._portrait = portrait
        self.main_box.setDirection(QtWidgets.QBoxLayout.TopToBottom if portrait
                                   else QtWidgets.QBoxLayout.LeftToRight)
        for w in self.readouts + self.buttons:
            self.ro_grid.removeWidget(w); self.btn_grid.removeWidget(w)
        rcols = 4 if portrait else 1
        for i, w in enumerate(self.readouts):
            self.ro_grid.addWidget(w, i // rcols, i % rcols)
        bcols = 4 if portrait else 2
        for i, w in enumerate(self.buttons):
            self.btn_grid.addWidget(w, i // bcols, i % bcols)
        panel = self.main_box.itemAt(1).widget()
        panel.setMaximumWidth(16777215 if portrait else 340)

    def resizeEvent(self, ev):
        super().resizeEvent(ev)
        self._relayout()

    # -- settings ------------------------------------------------------------
    def _apply_settings(self):
        s = self.s
        self.parser_component = s.get("component")
        self.em_parser = EM31Parser(s.get("component"), s.get("short_boom"))
        self.nmea = NMEAReader()
        if not hasattr(self, "fuser"):
            self.fuser = Fuser()
        f = self.fuser
        f.gps_latency, f.em_latency = s.get("gps_latency"), s.get("em_latency")
        f.layback, f.cross_offset, f.max_gap = s.get("layback"), s.get("cross_offset"), s.get("max_gap")
        self.min_quality = s.get("min_quality")
        name = s.get("cmap") if s.get("cmap") in pg.colormap.listMaps() else "viridis"
        cmap = pg.colormap.get(name)
        self.cmap = cmap
        self.cbar.setColorMap(cmap)
        self.lut_brushes = [pg.mkBrush(c) for c in cmap.getLookupTable(nPts=256, mode="qcolor")]
        self.t_grid.setInterval(int(s.get("grid_interval") * 1000))
        self.t_grid.start()
        self.img.setVisible(s.get("show_grid"))
        self.scatter.setVisible(s.get("show_points"))
        self.img.setOpacity(s.get("grid_opacity")); self.scatter.setOpacity(s.get("grid_opacity"))
        url = s.get("tile_url") if s.get("tile_source") == CUSTOM_SRC else im.TILE_SOURCES.get(s.get("tile_source"))
        cache = im.TileCache(self._tile_root(), url) if url else None
        if self.tile_layer is None:
            self.tile_layer = im.TileLayer(self.map.getViewBox(), cache)
        elif cache is None or self.tile_layer.cache is None or cache.dir != self.tile_layer.cache.dir:
            self.tile_layer.set_cache(cache)
        self.tile_layer.visible = s.get("tiles_on")
        self.tile_layer.set_opacity(s.get("imagery_opacity"))
        for it in self.file_items:
            it.setOpacity(s.get("imagery_opacity"))
        self.t_tiles.start()
        if s.get("map_dipole") in self.mode_actions:
            self.mode_actions[s.get("map_dipole")].setChecked(True)
        self._shown_mode = None
        self.btn_follow.blockSignals(True); self.btn_follow.setChecked(s.get("follow"))
        self.btn_follow.blockSignals(False)
        self._dirty = True
        self.last_grid = None
        self._regrid()

    def _settings(self):
        dlg = SettingsDialog(self.s, self)
        if dlg.exec():
            was_connected = self.btn_connect.isChecked()
            dlg.save()
            self._apply_settings()
            if was_connected and not self.btn_record.isChecked():
                self.btn_connect.setChecked(False); self.btn_connect.setChecked(True)
            elif was_connected:
                self.status.setText("Port changes take effect after Disconnect/Connect.")

    def _set_follow(self, on, save=False):
        if self.btn_follow.isChecked() != on:
            self.btn_follow.blockSignals(True); self.btn_follow.setChecked(on)
            self.btn_follow.blockSignals(False)
        if save or not on:
            self.s.set("follow", on)

    # -- connect / record ------------------------------------------------------
    def _toggle_connect(self, on):
        if on:
            self.fuser.reset()
            self.em_parser = EM31Parser(self.s.get("component"), self.s.get("short_boom"))
            self.nmea = NMEAReader()
            if self.s.get("simulate"):
                walker = SimWalker()
                vmd = lambda: self.s.get("dipole_expected") == "VMD"
                self.sources = [SimSource("em", walker, self.q, 10.9, vertical=vmd),
                                SimSource("gps", walker, self.q, 5.0)]
            else:
                self.sources = []
                if self.s.get("em_port"):
                    self.sources.append(SerialSource("em", self.s.get("em_port"),
                                                     self.s.get("em_baud"), self.q))
                if self.s.get("gps_port"):
                    self.sources.append(SerialSource("gps", self.s.get("gps_port"),
                                                     self.s.get("gps_baud"), self.q))
                if not self.sources:
                    QtWidgets.QMessageBox.information(
                        self, "No ports", "Choose the EM31 and GPS COM ports in Settings → Ports "
                        "(or enable the simulator).")
                    self.btn_connect.setChecked(False)
                    return
            for src in self.sources:
                src.start()
            self.btn_connect.setText("■ Disconnect")
            self.btn_record.setEnabled(True)
        else:
            if self.btn_record.isChecked():
                self.btn_record.setChecked(False)
            for src in self.sources:
                src.stop()
            self.sources = []
            self.btn_connect.setText("▶ Connect")
            self.btn_record.setEnabled(False)

    def _toggle_record(self, on):
        if on:
            try:
                self.logger = SurveyLogger(self.s.get("out_folder"))
            except OSError as e:
                QtWidgets.QMessageBox.critical(self, "Cannot write", str(e))
                self.btn_record.setChecked(False)
                return
            self.btn_record.setText("■ Stop rec")
            self.status.setText(f"Recording to {self.logger.csv_path}")
        else:
            if self.logger:
                for pr in self.fuser.flush_all():
                    self._handle_positioned(pr)
                self.logger.close()
                self.status.setText(f"Saved {self.logger.count} readings → {self.logger.csv_path}")
                self.logger = None
            self.btn_record.setText("● Record")

    def _mark(self):
        self.mark_no += 1
        t = time.monotonic()
        if self.logger:
            self.logger.comment(t, self._line_label(), f"MARK {self.mark_no}")
        if self.cur_pos is not None:
            e, n = self.cur_pos
            self.marks_e.append(e); self.marks_n.append(n)
            self.marks.setData(self.marks_e, self.marks_n)
        self.status.setText(f"Mark {self.mark_no}")

    def _line_label(self):
        if self.plan and self.active_line is not None:
            return self.plan.lines[self.active_line].name
        return str(self.line_no)

    def _new_line(self):
        if self.plan and self.plan.lines:
            self._step_line(+1)
            return
        self.line_no += 1
        if self.logger:
            self.logger.comment(time.monotonic(), self._line_label(), f"LINE {self.line_no}")
        self.status.setText(f"Line {self.line_no}")

    def _step_line(self, d):
        if not (self.plan and self.plan.lines):
            return
        n = len(self.plan.lines)
        self.active_line = 0 if self.active_line is None else (self.active_line + d) % n
        self._show_active_line()
        if self.logger:
            self.logger.comment(time.monotonic(), self._line_label(), f"LINE {self._line_label()}")
        self.status.setText(f"Planned line {self._line_label()} ({self.active_line + 1}/{n})")

    # -- data pump -------------------------------------------------------------
    def _drain(self):
        positioned = []
        n = 0
        while n < 2000:
            try:
                kind, t, data = self.q.get_nowait()
            except queue.Empty:
                break
            n += 1
            if kind == "em":
                if self.logger:
                    self.logger.raw_em(data)
                if self.monitor and self.monitor.isVisible():
                    self.monitor.add_em(data)
                for r in self.em_parser.feed(data, t):
                    self.last_em, self.last_em_t = r, time.monotonic()
                    self.em_times.append(r.t)
                    self.prof_t.append(r.t); self.prof_c.append(r.cond); self.prof_i.append(r.inphase)
                    if self.logger:
                        positioned += self.fuser.add_em(r)
            elif kind == "gps":
                fixes, lines = self.nmea.feed(data, t)
                for ln in lines:
                    if self.logger:
                        self.logger.raw_nmea(t, ln)
                    if self.monitor and self.monitor.isVisible():
                        self.monitor.add_gps(ln)
                for fx in fixes:
                    self.last_fix, self.last_fix_t = fx, time.monotonic()
                    if not quality_ok(fx.quality, self.min_quality):
                        continue
                    first = self.fuser.zone is None
                    positioned += self.fuser.add_fix(fx)
                    if first:
                        self._zone_changed()
                    if self.fuser._fixes:
                        _, e, nn, _ = self.fuser._fixes[-1]
                        self.track.append((e, nn))
                        self.cur_pos = self.fuser.sensor_position(e, nn)
                        self._update_guidance()
            elif kind.endswith("_status"):
                msg = data.decode(errors="replace")
                if msg != "open":
                    self.status.setText(f"{kind[:-7].upper()} port: {msg} — retrying…")
        for pr in positioned:
            self._handle_positioned(pr)

    def _handle_positioned(self, pr):
        if self.logger:
            self.logger.write(pr, self._line_label(), self.zone, depth=self._depth(pr.em.vertical))
        if pr.east is not None:
            self.pe.append(pr.east); self.pn.append(pr.north)
            self.pc.append(np.nan if pr.em.cond is None else pr.em.cond)
            self.pi.append(np.nan if pr.em.inphase is None else pr.em.inphase)
            self.pv.append(bool(pr.em.vertical))
            self._dirty = True

    # -- drawing ---------------------------------------------------------------
    def _depth(self, vertical):
        return self.s.get("depth_vmd") if vertical else self.s.get("depth_hmd")

    def _map_mode(self):
        """True = VMD, False = HMD, or DIFF."""
        m = self.s.get("map_dipole")
        if m == DIFF:
            return DIFF
        if m == "auto":
            if self.last_em is not None:
                return bool(self.last_em.vertical)
            return self.s.get("dipole_expected") == "VMD"
        return m == "VMD"

    def _set_map_dipole(self, key):
        self.s.set("map_dipole", key)
        self._shown_mode = None
        self._redraw(); self._regrid()

    def _values(self):
        return np.asarray(self.pc if self.s.get("channel") == "cond" else self.pi, float)

    def _map_arrays(self, mode=None):
        """E, N, values of the points in one dipole mode (default: the mapped one)."""
        mode = self._map_mode() if mode is None else mode
        if mode == DIFF:
            return np.array([]), np.array([]), np.array([])
        mask = np.asarray(self.pv, bool) == mode
        return np.asarray(self.pe)[mask], np.asarray(self.pn)[mask], self._values()[mask]

    def _check_mode_switch(self):
        mode = self._map_mode()
        if mode == self._shown_mode:
            return
        self._shown_mode = mode
        ch = "σa" if self.s.get("channel") == "cond" else "In-phase"
        unit = "mS/m" if self.s.get("channel") == "cond" else "ppt"
        if mode == DIFF:
            self.cbar.axis.setLabel(f"{ch} VMD − HMD ({unit})")
            self.cbar.setColorMap(pg.colormap.get(DIFF_CMAP))
        else:
            self.cbar.axis.setLabel(f"{ch} {mode_name(mode)} ~{self._depth(mode):g} m ({unit})")
            self.cbar.setColorMap(self.cmap)
        self.scatter.clear(); self.img.clear(); self.last_grid = None
        self._levels = None
        self._dirty = True
        if self.grid_future is not None:
            self.grid_future.cancel(); self.grid_future = None
        QtCore.QTimer.singleShot(0, self._regrid)

    def _compute_levels(self, v):
        if not self.s.get("auto_range"):
            return self.s.get("vmin"), self.s.get("vmax")
        v = v[np.isfinite(v)]
        if len(v) < 2:
            return None
        lo, hi = np.percentile(v, [2, 98])
        if hi - lo < 1e-6:
            hi = lo + 1.0
        return float(lo), float(hi)

    def _redraw(self):
        # profile strip
        now = time.monotonic()
        while self.prof_t and now - self.prof_t[0] > PROFILE_SECONDS:
            self.prof_t.popleft(); self.prof_c.popleft(); self.prof_i.popleft()
        if self.prof_t:
            tt = np.asarray(self.prof_t) - now
            self.c_c.setData(tt, np.array(self.prof_c, dtype=float))
            self.c_i.setData(tt, np.array(self.prof_i, dtype=float))
            self.p_c.setXRange(-PROFILE_SECONDS, 0, padding=0)

        # current position + track
        if self.track:
            tr = np.asarray(self.track)
            self.track_curve.setData(tr[:, 0], tr[:, 1])
            self.here.setData([tr[-1, 0]], [tr[-1, 1]])
            if self.btn_follow.isChecked():
                vb = self.map.getViewBox()
                (x0, x1), (y0, y1) = vb.viewRange()
                cx, cy = tr[-1]
                if not (x0 + 0.2 * (x1 - x0) < cx < x1 - 0.2 * (x1 - x0)
                        and y0 + 0.2 * (y1 - y0) < cy < y1 - 0.2 * (y1 - y0)):
                    w, h = x1 - x0, y1 - y0           # keep the user's zoom level
                    if not getattr(self, "_view_init", False) or w > 1e5:
                        w = h = 60                     # first fix: start with a 60 m view
                    self._view_init = True
                    vb.setRange(xRange=(cx - w / 2, cx + w / 2), yRange=(cy - h / 2, cy + h / 2),
                                padding=0)

        self._check_mode_switch()
        if not self._dirty:
            return
        self._dirty = False
        if self._map_mode() == DIFF:
            return                       # difference exists only as a grid
        e_all, n_all, v = self._map_arrays()
        lv = self._compute_levels(v)
        if lv:
            self._levels = lv
            self.cbar.setLevels(lv)
        if self.s.get("show_points") and len(v) and self._levels:
            e = e_all[-MAX_SCATTER:]; n = n_all[-MAX_SCATTER:]
            vv = v[-MAX_SCATTER:]
            lo, hi = self._levels
            idx = np.clip(((vv - lo) / (hi - lo) * 255), 0, 255)
            idx = np.where(np.isfinite(idx), idx, 0).astype(int)
            self.scatter.setData(e, n, brush=[self.lut_brushes[i] for i in idx])

    def _regrid(self):
        if not self.s.get("show_grid") or len(self.pe) < 10:
            return
        if self.grid_future is not None and not self.grid_future.done():
            return
        if self.grid_future is not None:
            self._accept_grid()
        mode = self._map_mode()
        if mode == DIFF:
            ev, nv, vv = self._map_arrays(True)
            eh, nh, vh = self._map_arrays(False)
            if len(vv) < 10 or len(vh) < 10:
                self.status.setText("Difference map needs both VMD and HMD readings over the area.")
                return
            fut = self.pool.submit(diff_grid, ev, nv, vv, eh, nh, vh,
                                   self.s.get("cell"), self.s.get("radius"))
        else:
            e, n, v = self._map_arrays()
            if len(v) < 10:
                return
            fut = self.pool.submit(idw_grid, e, n, v, self.s.get("cell"), self.s.get("radius"))
        fut.mode = mode
        self.grid_future = fut
        QtCore.QTimer.singleShot(150, self._poll_grid)

    def _poll_grid(self):
        if self.grid_future is None:
            return
        if self.grid_future.done():
            self._accept_grid()
        else:
            QtCore.QTimer.singleShot(150, self._poll_grid)

    def _accept_grid(self):
        fut, self.grid_future = self.grid_future, None
        if fut.cancelled() or getattr(fut, "mode", None) != self._map_mode():
            return                       # a grid for the other dipole mode; discard
        try:
            res = fut.result()
        except Exception:
            traceback.print_exc()
            return
        if res is None:
            return
        grid, x0, y0, cell = res
        self.last_grid = res
        if fut.mode == DIFF:
            if self.s.get("auto_range"):
                g = grid[np.isfinite(grid)]
                a = float(np.percentile(np.abs(g), 98)) if len(g) else 1.0
                self._levels = (-max(a, 1e-3), max(a, 1e-3))     # symmetric about zero
            else:
                self._levels = (self.s.get("vmin"), self.s.get("vmax"))
            self.cbar.setLevels(self._levels)
        self.img.setImage(grid.T, autoLevels=False)          # ImageItem is [x, y]
        self.img.setRect(QtCore.QRectF(x0, y0, grid.shape[1] * cell, grid.shape[0] * cell))
        if self._levels:
            self.cbar.setLevels(self._levels)

    def _zoom_center(self):
        if self.btn_follow.isChecked() and self.cur_pos is not None:
            return self.cur_pos
        return None

    def _fit(self):
        self._view_init = True
        self._set_follow(False, save=True)
        if self.pe:
            e, n = np.asarray(self.pe), np.asarray(self.pn)
            self.map.getViewBox().setRange(xRange=(e.min(), e.max()), yRange=(n.min(), n.max()))
        elif self.plan is not None:
            e0, e1, n0, n1 = self.plan.bounds()
            self.map.getViewBox().setRange(xRange=(e0 - 5, e1 + 5), yRange=(n0 - 5, n1 + 5))
        elif self.track:
            self.map.getViewBox().autoRange()

    # -- status ----------------------------------------------------------------
    def _update_status(self):
        now = time.monotonic()
        connected = self.btn_connect.isChecked()
        em_ok = self.last_em is not None and now - self.last_em_t < 2.0
        if self.last_em and em_ok:
            r = self.last_em
            self.v_cond.setText("—" if r.cond is None else f"{r.cond:.1f}")
            self.v_inph.setText("—" if r.inphase is None else f"{r.inphase:.2f}")
            rate = 0.0
            if len(self.em_times) > 2:
                rate = (len(self.em_times) - 1) / max(1e-3, self.em_times[-1] - self.em_times[0])
            mode = mode_name(r.vertical)
            expected = self.s.get("dipole_expected")
            if mode != expected:
                self.s_cond.setText(f"⚠ {mode} — survey set to {expected}")
                self.s_cond.setStyleSheet("color:white;background:#c62828;font-size:11px;font-weight:700")
            else:
                self.s_cond.setText(f"{mode} ~{self._depth(r.vertical):g} m · rng {r.range} · {rate:.1f} Hz")
                self.s_cond.setStyleSheet("color:#444;font-size:11px")
            self.s_inph.setText("MARKER" if r.marker else "")
            for w in (self.v_cond, self.v_inph):
                w.setStyleSheet("font-size:24px;font-weight:600;color:#111")
        elif connected:
            self.v_cond.setText("NO DATA"); self.v_inph.setText("—")
            self.v_cond.setStyleSheet("font-size:20px;font-weight:700;color:#c62828")
            self.s_cond.setText("check cable / power / port")

        fx = self.last_fix
        gps_ok = fx is not None and now - self.last_fix_t < 3.0
        if gps_ok:
            q = QUALITY_NAMES.get(fx.quality, str(fx.quality))
            good = quality_ok(fx.quality, self.min_quality)
            self.v_gps.setText(q)
            self.v_gps.setStyleSheet("font-size:22px;font-weight:600;color:"
                                     + ("#2e7d32" if good else "#ef6c00"))
            pd = f" · PDOP {self.nmea.pdop:.1f}" if self.nmea.pdop else ""
            self.s_gps.setText(f"{fx.nsat} sats · HDOP {fx.hdop or 0:.1f}{pd}")
        elif connected:
            self.v_gps.setText("NO FIX")
            self.v_gps.setStyleSheet("font-size:20px;font-weight:700;color:#c62828")
            self.s_gps.setText("waiting for GGA…")

        if self.logger:
            self.v_log.setText(f"{self.logger.count}")
            self.v_log.setStyleSheet("font-size:24px;font-weight:600;color:#c62828")
            self.s_log.setText(self._log_summary())
        else:
            self.v_log.setText("off"); self.v_log.setStyleSheet("font-size:24px;color:#666")
            self.s_log.setText(self._log_summary())

    def _log_summary(self):
        nv = int(np.sum(self.pv)); nh = len(self.pv) - nv
        return f"line {self._line_label()} · VMD {nv} · HMD {nh}"

    # -- menu actions ------------------------------------------------------------
    def _show_monitor(self):
        if self.monitor is None:
            self.monitor = MonitorDialog(self)
        self.monitor.show(); self.monitor.raise_()

    def _clear_map(self):
        self.pe.clear(); self.pn.clear(); self.pc.clear(); self.pi.clear(); self.pv.clear()
        self.marks_e.clear(); self.marks_n.clear(); self.marks.setData([], [])
        self.track.clear(); self.track_curve.setData([], [])
        self.scatter.clear(); self.img.clear(); self.last_grid = None
        self._dirty = True

    def _load_csv(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load survey CSV", self.s.get("out_folder"), "CSV (*.csv)")
        if not path:
            return
        n = 0
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    e, nn = float(row["east"]), float(row["north"])
                except (ValueError, KeyError, TypeError):
                    continue
                self.pe.append(e); self.pn.append(nn)
                self.pc.append(float(row["cond_mS_m"]) if row.get("cond_mS_m") else np.nan)
                self.pi.append(float(row["inphase_ppt"]) if row.get("inphase_ppt") else np.nan)
                self.pv.append((row.get("dipole") or "V").upper() in ("V", "VMD"))
                if self.zone is None and row.get("utm_zone"):
                    self.zone = int(row["utm_zone"]); self._zone_changed()
                n += 1
        self.status.setText(f"Loaded {n} points from {os.path.basename(path)}")
        self._shown_mode = None
        self._dirty = True
        self._regrid()
        self._fit()

    def _export_grid(self):
        if self.last_grid is None or self.zone is None:
            QtWidgets.QMessageBox.information(self, "No grid", "No grid has been computed yet.")
            return
        ch = f'{self.s.get("channel")}_{mode_name(self._map_mode()).replace("−", "-")}'
        default = os.path.join(self.s.get("out_folder"),
                               time.strftime(f"grid_{ch}_%Y%m%d_%H%M%S.asc"))
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Export grid", default, "ESRI ASCII grid (*.asc)")
        if path:
            grid, x0, y0, cell = self.last_grid
            write_ascii_grid(path, grid, x0, y0, cell, self.zone, self.fuser.north_hemi)
            self.status.setText(f"Grid saved: {path} (UTM {self.zone}, WGS84)")

    def _export_png(self):
        default = os.path.join(self.s.get("out_folder"), time.strftime("map_%Y%m%d_%H%M%S.png"))
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Export map image", default, "PNG (*.png)")
        if path:
            self.map.grab().save(path)
            self.status.setText(f"Image saved: {path}")

    # -- planned lines ------------------------------------------------------------
    def _ensure_zone(self, lat, lon):
        if self.zone is None:
            from .geo import utm_zone
            self.fuser.zone = utm_zone(lat, lon)
            self.fuser.north_hemi = lat >= 0
            self._zone_changed()

    def _zone_changed(self):
        if self.tile_layer:
            self.tile_layer.zone, self.tile_layer.north = self.zone, self.fuser.north_hemi
        self.t_tiles.start()

    def _load_plan_dialog(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Load planned lines / boundary (select one or more files)",
            self._last_plan_dir(), FILE_FILTER)
        if paths:
            self._load_plan_sources([{"path": p, "layers": None} for p in paths], fit=True, ask=True)

    def _last_plan_dir(self):
        try:
            srcs = json.loads(self.s.get("plan_sources") or "[]")
            if srcs:
                return os.path.dirname(srcs[0]["path"])
        except (ValueError, KeyError, TypeError):
            pass
        return self.s.get("out_folder")

    def _read_layers_zone(self, path, ask):
        try:
            return read_layers(path, zone=self.zone)
        except PlanError as e:
            if "need a zone" in str(e) and ask:
                z, ok = QtWidgets.QInputDialog.getInt(
                    self, "UTM zone", f"UTM zone (north) of the coordinates in {os.path.basename(path)}:",
                    self.zone or 11, 1, 60)
                if ok:
                    return read_layers(path, zone=z)
                return None
            raise

    def _load_plan_sources(self, sources, fit=False, ask=False, quiet=False):
        """sources: [{"path": str, "layers": [names] | None}] -> one combined plan."""
        chosen, saved, problems = [], [], []
        for src in sources:
            path = src["path"]
            try:
                layers = self._read_layers_zone(path, ask)
            except Exception as e:
                problems.append(f"{os.path.basename(path)}: {e}")
                continue
            if layers is None:
                continue
            names = src.get("layers")
            if names is not None:
                keep = [l for l in layers if l.name in names]
            else:
                sel = default_selection(layers)
                if len(layers) > 1 and ask:
                    dlg = LayerDialog(path, layers, sel, self)
                    if not dlg.exec():
                        continue
                    keep = dlg.selected()
                else:
                    keep = [l for l, on in zip(layers, sel) if on]
            if keep:
                chosen += keep
                saved.append({"path": path, "layers": [l.name for l in keep] if len(layers) > 1 else None})
        if problems and not quiet:
            QtWidgets.QMessageBox.warning(self, "Some files weren't loaded", "\n\n".join(problems))
        if not chosen:
            return False
        plan = plan_from_layers(chosen)
        self._ensure_zone(*plan.center_latlon())
        self.plan = plan.project(self.zone, self.fuser.north_hemi)
        self.s.set("plan_sources", json.dumps(saved))
        self.s.set("plan_path", "")
        self.active_line = 0 if self.plan.lines else None
        if self.cur_pos is not None and self.plan.lines:
            self.active_line = self.plan.nearest(*self.cur_pos)
        self._draw_plan()
        bits = []
        if self.plan.lines:
            bits.append(f"{len(self.plan.lines)} lines ({sum(l.length for l in self.plan.lines):.0f} m)")
        if self.plan.polygons:
            bits.append(f"{len(self.plan.polygons)} boundary")
        if self.plan.points:
            bits.append(f"{len(self.plan.points)} points")
        files = ", ".join(os.path.basename(x["path"]) for x in saved)
        self.status.setText(f"Plan: {', '.join(bits)} — {files}")
        if fit:
            self._fit_plan()
        return True

    def _load_plan(self, path, fit=False, quiet=False):
        """Single-file entry point (kept for older settings and scripts)."""
        return self._load_plan_sources([{"path": path, "layers": None}], fit=fit, quiet=quiet)

    def _fit_plan(self):
        if self.plan is None:
            return
        self._set_follow(False, save=True)
        self._view_init = True
        e0, e1, n0, n1 = self.plan.bounds()
        self.map.getViewBox().setRange(xRange=(e0 - 10, e1 + 10), yRange=(n0 - 10, n1 + 10))

    def _draw_plan(self):
        for it in self.plan_items:
            self.map.removeItem(it)
        self.plan_items = []
        self.line_labels = []
        if not self.plan:
            for it in (self.plan_curve, self.plan_active, self.plan_wpts, self.plan_bounds):
                it.setData([], [])
            self.banner.hide()
            return
        xs, ys = [], []
        for ln in self.plan.lines:
            xs += list(ln.e) + [np.nan]; ys += list(ln.n) + [np.nan]
        self.plan_curve.setData(np.array(xs, float), np.array(ys, float))
        bx, by = [], []
        for _, rings in self.plan.poly_en:
            for e, n in rings:
                bx += list(e) + [e[0], np.nan]; by += list(n) + [n[0], np.nan]
        self.plan_bounds.setData(np.array(bx, float), np.array(by, float))
        if self.plan.pe is not None:
            self.plan_wpts.setData(self.plan.pe, self.plan.pn)
        else:
            self.plan_wpts.setData([], [])
        self.line_labels = []
        if 0 < len(self.plan.lines) <= 400:
            for ln in self.plan.lines:
                t = pg.TextItem(ln.name, color="#004d40", fill=pg.mkBrush(255, 255, 255, 170),
                                anchor=(0.5, 1.2))
                t.setPos(ln.e[0], ln.n[0]); t.setZValue(9)
                self.map.addItem(t, ignoreBounds=True); self.plan_items.append(t)
                self.line_labels.append(t)
            starts = np.array([[ln.e[0], ln.n[0]] for ln in self.plan.lines])
            gaps = np.hypot(*np.diff(starts, axis=0).T) if len(starts) > 1 else np.array([1e9])
            self.label_spacing = max(float(np.median(gaps)), 0.01)
            self._thin_labels()
        for i, (nm, _, _) in enumerate(self.plan.points if len(self.plan.points) <= 40 else []):
            t = pg.TextItem(nm, color="#5d4037", anchor=(-0.2, 0.5))
            t.setPos(self.plan.pe[i], self.plan.pn[i]); t.setZValue(9)
            self.map.addItem(t, ignoreBounds=True); self.plan_items.append(t)
        self._show_active_line()

    def _thin_labels(self):
        """Show every k-th line label so labels never overlap on screen."""
        labels = getattr(self, "line_labels", [])
        if not labels:
            return
        px = self.map.getViewBox().viewPixelSize()[0] or 1.0
        k = max(1, int(math.ceil(45 * px / self.label_spacing)))
        for i, t in enumerate(labels):
            t.setVisible(i % k == 0 or i == self.active_line)

    def _show_active_line(self):
        if self.plan and self.active_line is not None and self.plan.lines:
            ln = self.plan.lines[self.active_line]
            self.plan_active.setData(ln.e, ln.n)
        else:
            self.plan_active.setData([], [])
        self._thin_labels()
        self._update_guidance()

    def _clear_plan(self):
        self.plan = None; self.active_line = None
        self.s.set("plan_path", ""); self.s.set("plan_sources", "")
        self._draw_plan()

    def _map_clicked(self, ev):
        self._set_follow(False)
        if ev.double():
            return
        pt = self.map.getViewBox().mapSceneToView(ev.scenePos())
        self._select_line_at(pt.x(), pt.y())

    def _tap_select(self, x, y):
        self._select_line_at(x, y)

    def _select_line_at(self, x, y):
        if not (self.plan and self.plan.lines):
            return
        idx = self.plan.nearest(x, y)
        d = self.plan._project_onto(self.plan.lines[idx], x, y)[0]
        px = self.map.getViewBox().viewPixelSize()[0]
        if d <= 30 * px and idx != self.active_line:       # tap within ~30 px selects a line
            self.active_line = idx
            self._show_active_line()
            if self.logger:
                self.logger.comment(time.monotonic(), self._line_label(), f"LINE {self._line_label()}")
            self.status.setText(f"Planned line {self._line_label()} selected")

    def _update_guidance(self):
        if not (self.plan and self.plan.lines):
            self.banner.hide(); return
        self.banner.show()
        if self.cur_pos is None:
            self._banner(f"Line {self._line_label()} — waiting for GPS", "#607d8b"); return
        if self.s.get("line_mode") == "nearest" or self.active_line is None:
            self.active_line = self.plan.nearest(*self.cur_pos)
            ln = self.plan.lines[self.active_line]
            if self.plan_active.xData is None or len(self.plan_active.xData) != len(ln.e) \
                    or self.plan_active.xData[0] != ln.e[0]:
                self.plan_active.setData(ln.e, ln.n)
        g = self.plan.guidance(self.active_line, *self.cur_pos, self.fuser._heading)
        tol = self.s.get("line_tol")
        a = abs(g.offset)
        if a > 25:
            self._banner(f"Line {g.name} is {a:.0f} m away", "#607d8b"); return
        if g.remaining < 1.5:
            self._banner(f"END OF LINE {g.name}  —  tap New line", "#1565c0"); return
        arrow = "◀" if g.offset > 0 else "▶"
        col = "#2e7d32" if a <= tol else ("#ef6c00" if a <= 3 * tol else "#c62828")
        steer = "ON LINE" if a <= tol else f"{arrow} {a:.2f} m"
        if g.has_turns and g.to_turn < g.remaining - 0.5:
            if g.to_turn < 1.5:
                self._banner(f"TURN — follow track {g.name}", "#1565c0"); return
            dist = f"{g.to_turn:.0f} m to turn"
        else:
            dist = f"{g.remaining:.0f} m to go"
        self._banner(f"{steer}    Line {g.name}    {dist}", col)

    def _banner(self, text, color):
        self.banner.setText(text)
        self.banner.setStyleSheet(f"background:{color};color:white;font-size:24px;"
                                  "font-weight:700;border-radius:6px;padding:4px")

    # -- imagery ---------------------------------------------------------------------
    def _tile_root(self):
        return os.path.join(self.s.get("out_folder"), "imagery_cache")

    def _update_tiles(self):
        self._thin_labels()
        if self.tile_layer is None:
            return
        self.tile_layer.zone, self.tile_layer.north = self.zone, self.fuser.north_hemi
        try:
            self.tile_layer.update(self.map.getViewBox().width())
        except Exception:
            traceback.print_exc()
        msg = self.tile_layer.message
        if msg:
            self.map.setTitle(f"<span style='color:#c62828'>{msg}</span>", size="9pt")
        else:
            self.map.setTitle(None)

    def _load_image_dialog(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Load imagery", self.s.get("out_folder"),
            "Images (*.tif *.tiff *.jpg *.jpeg *.png);;All files (*)")
        if path:
            self._load_image(path, fit=True)

    def _ask_crs(self):
        items = ["UTM (enter zone)", "WGS84 latitude / longitude", "Web Mercator (EPSG:3857)"]
        it, ok = QtWidgets.QInputDialog.getItem(self, "Image projection",
                                                "No .prj file found. What projection is the world file in?",
                                                items, 0, False)
        if not ok:
            return None
        if it.startswith("UTM"):
            z, ok = QtWidgets.QInputDialog.getInt(self, "UTM zone", "UTM zone (north):", self.zone or 12, 1, 60)
            return ("utm", z, True) if ok else None
        return ("geographic", None, True) if it.startswith("WGS") else ("webmercator", None, True)

    def _load_image(self, path, fit=False, quiet=False):
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
        try:
            assume = None
            saved = self.s.get("image_crs")
            if saved and self.s.get("image_path") == path:
                k, z, nh = saved.split(",")
                assume = (k, int(z) if z != "None" else None, nh == "True")
            try:
                if path.lower().endswith((".tif", ".tiff")):
                    try:
                        arr, ref = im.read_geotiff(path)
                    except im.ImageryError as e:
                        if "no GeoTIFF tags" not in str(e):
                            raise
                        arr, ref = im.read_world_file_image(path, assume)
                else:
                    arr, ref = im.read_world_file_image(path, assume)
            except im.ImageryError as e:
                if str(e) != "NEEDS_CRS" or quiet:
                    raise
                QtWidgets.QApplication.restoreOverrideCursor()
                assume = self._ask_crs()
                QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
                if assume is None:
                    return
                arr, ref = im.read_world_file_image(path, assume)
                self.s.set("image_crs", ",".join(map(str, assume)))
            self._ensure_zone(*im.image_center_latlon(arr, ref))
            self._clear_image(keep_setting=True)
            self.file_items = im.place_image(arr, ref, self.zone, self.fuser.north_hemi)
            vb = self.map.getViewBox()
            for it in self.file_items:
                it.setZValue(-90)
                it.setOpacity(self.s.get("imagery_opacity"))
                vb.addItem(it, ignoreBounds=True)
            self.s.set("image_path", path)
            self.status.setText(f"Imagery: {os.path.basename(path)} ({arr.shape[1]}×{arr.shape[0]} px)")
            if fit:
                self._set_follow(False, save=True)
                rect = self.file_items[0].mapRectToParent(self.file_items[0].boundingRect())
                for it in self.file_items[1:]:
                    rect = rect.united(it.mapRectToParent(it.boundingRect()))
                vb.setRange(rect)
        except Exception as e:
            if not quiet:
                QtWidgets.QMessageBox.warning(self, "Can't load imagery", str(e))
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

    def _clear_image(self, keep_setting=False):
        for it in self.file_items:
            self.map.getViewBox().removeItem(it)
        self.file_items = []
        if not keep_setting:
            self.s.set("image_path", "")

    def _download_dialog(self):
        if self.tile_layer is None or self.tile_layer.cache is None:
            QtWidgets.QMessageBox.information(self, "No source", "Set a custom URL in Settings → Layers first.")
            return
        dlg = DownloadDialog(self)
        if dlg.exec() != QtWidgets.QDialog.Accepted or not dlg.tiles:
            return
        cache = self.tile_layer.cache
        if dlg.center is not None:
            self._ensure_zone(*dlg.center)
        dl = im.Downloader(cache, dlg.tiles)
        prog = QtWidgets.QProgressDialog("Downloading imagery…", "Cancel", 0, len(dlg.tiles), self)
        prog.setWindowModality(QtCore.Qt.WindowModal); prog.setMinimumDuration(0)
        prog.setMinimumWidth(420)
        dl.start()
        while dl.is_alive():
            prog.setValue(dl.done)
            prog.setLabelText(f"Downloading imagery… {dl.done}/{len(dlg.tiles)} tiles"
                              + (f"  ({dl.failed} failed)" if dl.failed else ""))
            QtWidgets.QApplication.processEvents()
            if prog.wasCanceled():
                dl.cancelled = True
            time.sleep(0.05)
        prog.close()
        msg = f"{dl.done - dl.failed} tiles ready ({dl.missing} with no imagery)."
        if dl.failed:
            msg += f"\n{dl.failed} failed — last error: {dl.error}\nRun the download again to retry; finished tiles are kept."
        QtWidgets.QMessageBox.information(self, "Imagery download", msg)
        self.s.set("tiles_on", True); self.tile_layer.visible = True
        if dlg.fit_rect is not None:
            self._set_follow(False, save=True)
            self.map.getViewBox().setRange(xRange=dlg.fit_rect[0], yRange=dlg.fit_rect[1])
        self._update_tiles()

    def _restore_layers(self):
        try:
            srcs = [x for x in json.loads(self.s.get("plan_sources") or "[]") if os.path.exists(x["path"])]
        except (ValueError, KeyError, TypeError):
            srcs = []
        if srcs:
            self._load_plan_sources(srcs, fit=True, quiet=True)
        elif self.s.get("plan_path") and os.path.exists(self.s.get("plan_path")):
            self._load_plan(self.s.get("plan_path"), fit=True, quiet=True)
        if self.s.get("image_path") and os.path.exists(self.s.get("image_path")):
            self._load_image(self.s.get("image_path"), fit=self.plan is None, quiet=True)
        self._update_tiles()

    def closeEvent(self, ev):
        if self.logger:
            r = QtWidgets.QMessageBox.question(self, "Recording", "Stop recording and quit?")
            if r != QtWidgets.QMessageBox.Yes:
                ev.ignore(); return
        self.btn_record.setChecked(False)
        self.btn_connect.setChecked(False)
        self.pool.shutdown(wait=False, cancel_futures=True)
        super().closeEvent(ev)


# ---------------------------------------------------------------------------
def _install_excepthook():
    log_dir = os.path.join(os.path.expanduser("~"), "Documents", "EM31Mapper")
    os.makedirs(log_dir, exist_ok=True)

    def hook(et, ev, tb):
        msg = "".join(traceback.format_exception(et, ev, tb))
        with open(os.path.join(log_dir, "error_log.txt"), "a") as f:
            f.write(time.strftime("\n--- %Y-%m-%d %H:%M:%S ---\n") + msg)
        sys.__stderr__.write(msg)   # keep running: a field app should not die on one bad packet
    sys.excepthook = hook


def _run_selftest(app):
    """Headless-capable end-to-end check: simulator -> parse -> position -> log -> grid."""
    import tempfile
    global ORG
    ORG = "EM31Mapper-selftest"          # keep the user's real settings untouched
    out = tempfile.mkdtemp(prefix="em31selftest_")
    q = QtCore.QSettings(ORG, APP); q.clear()
    for k, v in {"simulate": True, "out_folder": out, "grid_interval": 1.0, "follow": True}.items():
        q.setValue(k, v)
    report = [f"EM31 Mapper {__version__} self-test", f"Python {sys.version.split()[0]}  Qt {QtCore.qVersion()}"]
    ok = True

    def check(name, fn):
        nonlocal ok
        try:
            r = fn()
            report.append(f"PASS  {name}" + (f": {r}" if r not in (None, True) else ""))
        except Exception as e:
            ok = False
            report.append(f"FAIL  {name}: {type(e).__name__}: {e}")

    check("serial ports", lambda: f"{len(list_ports())} found")
    check("GeoTIFF support", lambda: (__import__("tifffile").__version__, __import__("imagecodecs").__version__)[0])
    def codecs():
        import tifffile
        import numpy as _np
        img = (_np.arange(64 * 64 * 3) % 251).astype(_np.uint8).reshape(64, 64, 3)
        done = []
        for comp in ("lzw", "zlib", "jpeg", "zstd", "packbits"):
            fn = os.path.join(out, f"codec_{comp}.tif")
            tifffile.imwrite(fn, img, photometric="rgb", compression=comp)
            back = tifffile.imread(fn)
            if back.shape != img.shape or (comp != "jpeg" and not (back == img).all()):
                raise ValueError(f"{comp} round-trip mismatch")
            done.append(comp.upper())
        return ", ".join(done)
    check("GeoTIFF codecs", codecs)
    def plan_formats():
        from .plan import write_test_gpkg
        p = os.path.join(out, "selftest.gpkg")
        write_test_gpkg(p)
        pl = load_plan(p).project()
        ln = pl.lines[0]
        if pl.zone != 11 or not 140 < ln.length < 160 or not pl.polygons:
            raise ValueError(f"unexpected: zone {pl.zone}, length {ln.length:.1f}")
        return f"GeoPackage line {ln.length:.1f} m + boundary, UTM {pl.zone}N"
    check("plan formats", plan_formats)
    check("UTM maths", lambda: abs(__import__("em31mapper.geo", fromlist=["x"]).to_utm(45, -111, 12)[1] - 4982950.40) < 0.05 or 1 / 0)
    w = MainWindow(); w.resize(800, 1000); w.show()
    w.btn_connect.setChecked(True)
    QtCore.QTimer.singleShot(1500, lambda: w.btn_record.setChecked(True))

    def finish():
        nonlocal ok
        n = w.logger.count if w.logger else 0
        nmap = len(w.pe)
        grid = w.last_grid
        check("EM31 stream decoded", lambda: f"{n} readings logged" if n > 50 else 1 / 0)
        check("GPS positioning", lambda: f"{nmap} positioned" if nmap > 50 else 1 / 0)
        check("live grid", lambda: f"{grid[0].shape[1]}x{grid[0].shape[0]} cells" if grid is not None else 1 / 0)

        def zoom():
            w._set_follow(False)
            w0 = w.nav.view_width()
            w.nav.zoom(0.5); a = w.nav.view_width()
            w.nav.zoom(2.0); b = w.nav.view_width()
            if not (abs(a - w0 / 2) < 0.01 * w0 and abs(b - w0) < 0.01 * w0):
                raise ValueError(f"{w0:.1f} -> {a:.1f} -> {b:.1f}")
            return f"{w0:.0f} m -> {a:.0f} m -> {b:.0f} m, scale bar '{w.nav.scale.label()[2]}'"
        check("map zoom", zoom)
        path = w.logger.csv_path if w.logger else ""
        w.btn_record.setChecked(False); w.btn_connect.setChecked(False)
        check("CSV written", lambda: f"{os.path.getsize(path)} bytes" if path and os.path.getsize(path) > 1000 else 1 / 0)
        report.append("RESULT: " + ("ALL PASSED" if ok else "FAILED"))
        text = "\n".join(report)
        dest = os.path.join(os.path.expanduser("~"), "Documents", "EM31Mapper")
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, "selftest.txt"), "w") as f:
            f.write(text + "\n")
        if sys.stdout:
            print(text, flush=True)
        if "--quiet" not in sys.argv:
            QtWidgets.QMessageBox.information(w, "Self-test", text)
        q.clear()
        w.logger = None
        app.exit(0 if ok else 1)
    QtCore.QTimer.singleShot(14000, finish)


def main():
    if hasattr(QtCore.Qt, "HighDpiScaleFactorRoundingPolicy"):
        QtGui.QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            QtCore.Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP); app.setOrganizationName(ORG)
    f = app.font(); f.setPointSize(max(f.pointSize(), 11)); app.setFont(f)
    _install_excepthook()
    ico = os.path.join(os.path.dirname(os.path.abspath(__file__)), "em31mapper.ico")
    if os.path.exists(ico):
        app.setWindowIcon(QtGui.QIcon(ico))
    if "--selftest" in sys.argv:
        _run_selftest(app)
        sys.exit(app.exec())
    if "--simulate" in sys.argv:
        QtCore.QSettings(ORG, APP).setValue("simulate", True)
    w = MainWindow()
    scr = app.primaryScreen().availableGeometry()
    if scr.width() <= 1280:
        w.showMaximized()           # Mesa2 and similar tablets
    else:
        w.resize(900, 1000); w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
