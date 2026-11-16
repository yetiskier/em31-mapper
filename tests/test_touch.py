"""Touch navigation through Qt's real touch-event pipeline (offscreen)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6 import QtCore, QtTest  # noqa: E402
import pyqtgraph as pg  # noqa: E402

from em31mapper.mapnav import MapNavigator, nice_length  # noqa: E402


@pytest.fixture(scope="module")
def nav():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    plot = pg.PlotWidget(); plot.setAspectLocked(True); plot.resize(600, 800); plot.show()
    taps = []
    n = MapNavigator(plot, on_tap=lambda x, y: taps.append((x, y)))
    n.taps = taps
    for _ in range(20):
        app.processEvents()
    return n


def _rng(n):
    (x0, x1), (y0, y1) = n.vb.viewRange()
    return x0, x1, y0, y1


def _at(n, px, py):
    v = n.vb.mapSceneToView(n.plot.mapToScene(QtCore.QPoint(px, py)))
    return v.x(), v.y()


def _reset(n):
    n.vb.setRange(xRange=(0, 100), yRange=(0, 100), padding=0)
    QtWidgets.QApplication.processEvents()


def test_pan(nav):
    _reset(nav)
    vp, dev = nav.plot.viewport(), QtTest.QTest.createTouchDevice()
    c = vp.rect().center()
    x0, mpp, w0 = _rng(nav)[0], nav.vb.viewPixelSize()[0], _rng(nav)[1] - _rng(nav)[0]
    QtTest.QTest.touchEvent(vp, dev).press(0, c, vp).commit()
    for k in range(1, 11):
        QtTest.QTest.touchEvent(vp, dev).move(0, c + QtCore.QPoint(15 * k, 0), vp).commit()
    QtTest.QTest.touchEvent(vp, dev).release(0, c + QtCore.QPoint(150, 0), vp).commit()
    # axis tick labels can change width mid-pan and resize the plot by a pixel
    assert abs((x0 - _rng(nav)[0]) - 150 * mpp) < 1.0 * mpp
    assert abs((_rng(nav)[1] - _rng(nav)[0]) - w0) < 1.0 * mpp
    assert nav.taps == []                       # a drag is not a tap


def test_pinch_keeps_point_under_fingers(nav):
    _reset(nav)
    vp, dev = nav.plot.viewport(), QtTest.QTest.createTouchDevice()
    c = vp.rect().center() + QtCore.QPoint(40, -30)
    before = _at(nav, c.x(), c.y())
    w0 = _rng(nav)[1] - _rng(nav)[0]
    QtTest.QTest.touchEvent(vp, dev).press(0, c - QtCore.QPoint(40, 0), vp).press(1, c + QtCore.QPoint(40, 0), vp).commit()
    for k in range(1, 9):
        d = 40 + 10 * k
        QtTest.QTest.touchEvent(vp, dev).move(0, c - QtCore.QPoint(d, 0), vp).move(1, c + QtCore.QPoint(d, 0), vp).commit()
    QtTest.QTest.touchEvent(vp, dev).release(0, c - QtCore.QPoint(120, 0), vp).release(1, c + QtCore.QPoint(120, 0), vp).commit()
    assert abs(w0 / (_rng(nav)[1] - _rng(nav)[0]) - 3.0) < 1e-6        # 80 px -> 240 px apart
    after = _at(nav, c.x(), c.y())
    assert abs(after[0] - before[0]) < 1e-6 and abs(after[1] - before[1]) < 1e-6


def test_tap_reports_map_point(nav):
    _reset(nav)
    vp, dev = nav.plot.viewport(), QtTest.QTest.createTouchDevice()
    p = QtCore.QPoint(200, 300)
    want = _at(nav, p.x(), p.y())
    nav.taps.clear()
    QtTest.QTest.touchEvent(vp, dev).press(0, p, vp).commit()
    QtTest.QTest.touchEvent(vp, dev).release(0, p, vp).commit()
    assert len(nav.taps) == 1
    assert abs(nav.taps[0][0] - want[0]) < 1e-6 and abs(nav.taps[0][1] - want[1]) < 1e-6


def test_buttons_and_limits(nav):
    _reset(nav)
    w0 = _rng(nav)[1] - _rng(nav)[0]
    nav.btn_in.click()
    assert abs((_rng(nav)[1] - _rng(nav)[0]) - w0 / 2) < 1e-6
    nav.btn_out.click()
    assert abs((_rng(nav)[1] - _rng(nav)[0]) - w0) < 1e-6
    for _ in range(20):
        nav.zoom(0.5)                            # can't zoom in past 2 m across
    assert _rng(nav)[1] - _rng(nav)[0] >= 1.999


def test_scale_bar_lengths():
    assert [nice_length(x) for x in (0.7, 3.1, 7.9, 19, 120, 4100)] == [0.5, 2, 5, 10, 100, 2000]
