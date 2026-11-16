"""Touch-friendly map navigation for a pyqtgraph PlotWidget.

* one finger drag  -> pan          * two finger pinch -> zoom about the fingers
* quick tap        -> on_tap(x, y) in map coordinates
* on-map + / - buttons and a live scale bar
Mouse and stylus keep pyqtgraph's own behaviour (drag = pan, wheel = zoom).
"""
from __future__ import annotations

import math
import time

from PySide6 import QtCore, QtGui, QtWidgets

TAP_MOVE_PX = 12          # finger travel below this is a tap, not a pan
TAP_SECONDS = 0.8
MIN_VIEW_M = 2.0          # closest zoom: 2 m across
MAX_VIEW_M = 200_000.0    # farthest zoom: 200 km across

_TOUCH = (QtCore.QEvent.TouchBegin, QtCore.QEvent.TouchUpdate,
          QtCore.QEvent.TouchEnd, QtCore.QEvent.TouchCancel)


def nice_length(target: float) -> float:
    """Largest 1/2/5 x 10^k not exceeding target."""
    if not (target > 0 and math.isfinite(target)):
        return 0.0
    base = 10 ** math.floor(math.log10(target))
    for m in (5, 2, 1):
        if m * base <= target:
            return m * base
    return base


class ScaleBar(QtWidgets.QWidget):
    def __init__(self, parent, viewbox):
        super().__init__(parent)
        self.vb = viewbox
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents)
        self.resize(190, 34)
        viewbox.sigRangeChanged.connect(lambda *_: self.update())
        viewbox.sigResized.connect(lambda *_: self.update())

    def label(self):
        px = self.vb.viewPixelSize()[0]
        L = nice_length(120 * px)
        if L <= 0:
            return 0.0, 0.0, ""
        txt = f"{L / 1000:g} km" if L >= 1000 else (f"{L:g} m" if L >= 1 else f"{L * 100:g} cm")
        return L, L / px, txt

    def paintEvent(self, _):
        L, w, txt = self.label()
        if L <= 0:
            return
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        x0, y = 8, 24
        p.setPen(QtCore.Qt.NoPen)
        p.setBrush(QtGui.QColor(255, 255, 255, 200))
        p.drawRoundedRect(QtCore.QRectF(2, 2, w + 16, 30), 4, 4)
        pen = QtGui.QPen(QtGui.QColor("#111"), 3)
        pen.setCapStyle(QtCore.Qt.FlatCap)
        p.setPen(pen)
        p.drawLine(QtCore.QPointF(x0, y), QtCore.QPointF(x0 + w, y))
        p.drawLine(QtCore.QPointF(x0, y - 6), QtCore.QPointF(x0, y + 1))
        p.drawLine(QtCore.QPointF(x0 + w, y - 6), QtCore.QPointF(x0 + w, y + 1))
        f = p.font(); f.setPointSize(10); f.setBold(True); p.setFont(f)
        p.drawText(QtCore.QRectF(x0, 3, w, 16), QtCore.Qt.AlignCenter, txt)
        p.end()


class MapNavigator(QtCore.QObject):
    def __init__(self, plot, on_tap=None, on_user_nav=None, zoom_center=None):
        super().__init__(plot)
        self.plot = plot
        self.vb = plot.getViewBox()
        self.on_tap = on_tap or (lambda x, y: None)
        self.on_user_nav = on_user_nav or (lambda: None)
        self.zoom_center = zoom_center or (lambda: None)
        self.vb.setLimits(minXRange=MIN_VIEW_M, minYRange=MIN_VIEW_M,
                          maxXRange=MAX_VIEW_M, maxYRange=MAX_VIEW_M)
        vp = plot.viewport()
        vp.setAttribute(QtCore.Qt.WA_AcceptTouchEvents, True)
        vp.installEventFilter(self)
        plot.installEventFilter(self)
        self.btn_in = self._button("+", "Zoom in")
        self.btn_out = self._button("−", "Zoom out")
        self.btn_in.clicked.connect(lambda: self.zoom(0.5))
        self.btn_out.clicked.connect(lambda: self.zoom(2.0))
        self.scale = ScaleBar(plot, self.vb)
        self.vb.sigResized.connect(lambda *_: self._place())
        self._reset()
        QtCore.QTimer.singleShot(0, self._place)

    # -- widgets ---------------------------------------------------------------
    def _button(self, text, tip):
        b = QtWidgets.QToolButton(self.plot)
        b.setText(text); b.setToolTip(tip)
        b.setFixedSize(56, 56)
        b.setFocusPolicy(QtCore.Qt.NoFocus)
        b.setAutoRepeat(False)
        b.setStyleSheet("QToolButton{background:rgba(255,255,255,225);color:#111;border:1px solid #777;"
                        "border-radius:8px;font-size:30px;font-weight:700}"
                        "QToolButton:pressed{background:#cfd8dc}")
        return b

    def _place(self):
        r = self.vb.sceneBoundingRect()
        if r.width() <= 0:
            return
        tl = self.plot.viewport().mapTo(self.plot, self.plot.mapFromScene(r.topLeft()))
        bl = self.plot.viewport().mapTo(self.plot, self.plot.mapFromScene(r.bottomLeft()))
        self.btn_in.move(tl.x() + 8, tl.y() + 8)
        self.btn_out.move(tl.x() + 8, tl.y() + 8 + 62)
        self.scale.move(bl.x() + 6, bl.y() - self.scale.height() - 6)
        for w in (self.btn_in, self.btn_out, self.scale):
            w.raise_(); w.show()

    # -- zoom ------------------------------------------------------------------------
    def zoom(self, factor):
        c = self.zoom_center()
        if c is None:
            (x0, x1), (y0, y1) = self.vb.viewRange()
            c = ((x0 + x1) / 2, (y0 + y1) / 2)
        self.vb.scaleBy((factor, factor), center=QtCore.QPointF(*c))

    def view_width(self):
        (x0, x1), _ = self.vb.viewRange()
        return x1 - x0

    # -- touch -------------------------------------------------------------------------
    def _reset(self):
        self._start = None
        self._last = None
        self._pinch = None
        self._moved = False
        self._multi = False
        self._t0 = 0.0

    def _view(self, pos):
        sp = self.plot.mapToScene(QtCore.QPoint(int(round(pos.x())), int(round(pos.y()))))
        return self.vb.mapSceneToView(sp)

    def eventFilter(self, obj, ev):
        t = ev.type()
        if obj is self.plot:
            if t in (QtCore.QEvent.Resize, QtCore.QEvent.Show):
                QtCore.QTimer.singleShot(0, self._place)
            return False
        if t not in _TOUCH:
            return False
        if t == QtCore.QEvent.TouchCancel:
            self._reset(); ev.accept(); return True
        pts = [p for p in ev.points() if p.state() != QtGui.QEventPoint.State.Released]
        if t == QtCore.QEvent.TouchBegin:
            self._reset()
            self._t0 = time.monotonic()
            if pts:
                self._start = QtCore.QPointF(pts[0].position())
                self._last = QtCore.QPointF(self._start)
        if t in (QtCore.QEvent.TouchBegin, QtCore.QEvent.TouchUpdate) and pts:
            self._handle(pts)
        elif t == QtCore.QEvent.TouchEnd:
            if (not self._moved and not self._multi and self._start is not None
                    and time.monotonic() - self._t0 < TAP_SECONDS):
                v = self._view(self._start)
                self.on_tap(v.x(), v.y())
            self._reset()
        ev.accept()
        return True

    def _handle(self, pts):
        if len(pts) >= 2:
            self._multi = True
            a, b = pts[0].position(), pts[1].position()
            mid = QtCore.QPointF((a.x() + b.x()) / 2, (a.y() + b.y()) / 2)
            dist = max(math.hypot(a.x() - b.x(), a.y() - b.y()), 1.0)
            if self._pinch is not None:
                pmid, pdist = self._pinch
                anchor = self._view(pmid)                 # map point under the fingers
                s = pdist / dist
                self.vb.scaleBy((s, s), center=anchor)
                now = self._view(mid)
                self.vb.translateBy(x=anchor.x() - now.x(), y=anchor.y() - now.y())
                if not self._moved:
                    self._moved = True
                    self.on_user_nav()
            self._pinch = (mid, dist)
            self._last = None
            return
        p = QtCore.QPointF(pts[0].position())
        if self._pinch is not None or self._last is None:   # lifted a finger: continue as a pan
            self._pinch = None
            self._last = p
            return
        if not self._moved and self._start is not None:
            d = p - self._start
            if abs(d.x()) + abs(d.y()) < TAP_MOVE_PX:
                return
        a, b = self._view(self._last), self._view(p)
        self.vb.translateBy(x=a.x() - b.x(), y=a.y() - b.y())
        if not self._moved:
            self._moved = True
            self.on_user_nav()
        self._last = p
