#!/usr/bin/env python3
"""
Projector Mirror (Windows/Mac/Linux)
------------------------------------
Mirrors your PRIMARY display into a separate window that opens on the projector /
secondary screen. The window preserves the source aspect ratio during resize (corner
resize recommended) so the image never looks stretched.

Controls
========
- Drag window by title bar (or use normal OS move)
- Resize from corners; aspect ratio is preserved automatically
- F11  : Toggle borderless full screen on the current screen
- Space: Pause / resume the capture
- P    : Toggle "Pin on top" (always on top)
- +/-  : Increase / decrease target FPS (10–120)
- N    : Move window to the NEXT monitor
- 1    : Move window to PRIMARY monitor
- 2    : Move window to SECONDARY monitor (if exists)
- Q or Esc: Quit

Notes
=====
- Starts on the SECONDARY screen if present; otherwise uses the primary.
- Uses mss for high‑speed screen capture. If performance is low, reduce FPS with '-'.
- Aspect ratio is kept equal to the source (primary screen's native resolution).
- If you need perspective warping/keystone in the future, this can be extended.

Install:
    pip install PySide6 mss numpy

Run:
    python projector_mirror.py
"""
from __future__ import annotations

import sys
import time
from typing import Optional

import numpy as np
from mss import mss

from PySide6 import QtCore, QtGui, QtWidgets


def get_screens():
    app = QtWidgets.QApplication.instance()
    return app.screens()


class AspectRatioWidget(QtWidgets.QLabel):
    """
    QLabel subclass that enforces a given aspect ratio on resize
    and renders incoming QImages scaled with smooth transform.
    """
    def __init__(self, aspect_ratio: float, parent=None):
        super().__init__(parent)
        self.setAlignment(QtCore.Qt.AlignCenter)
        self.aspect_ratio = aspect_ratio  # width / height
        self._frame: Optional[QtGui.QImage] = None
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Expanding)
        # Set a neutral background
        pal = self.palette()
        pal.setColor(self.backgroundRole(), QtGui.QColor(0, 0, 0))
        self.setAutoFillBackground(True)
        self.setPalette(pal)

    def set_frame(self, img: QtGui.QImage):
        self._frame = img
        self.update()

    def sizeHint(self) -> QtCore.QSize:
        # Provide a decent default size with correct aspect
        h = 600
        w = int(h * self.aspect_ratio)
        return QtCore.QSize(w, h)

    def minimumSizeHint(self) -> QtCore.QSize:
        return QtCore.QSize(200, int(200 / max(self.aspect_ratio, 1e-6)))

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        # Enforce aspect ratio by adjusting either width or height
        w = event.size().width()
        h = event.size().height()
        target_h = int(w / self.aspect_ratio)
        target_w = int(h * self.aspect_ratio)
        # Choose whichever adjustment is closer to the intended new size
        if abs(target_h - h) <= abs(target_w - w):
            # adjust height
            self.setFixedSize(w, target_h)
        else:
            # adjust width
            self.setFixedSize(target_w, h)
        super().resizeEvent(event)

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        painter = QtGui.QPainter(self)
        rect = self.rect()
        painter.fillRect(rect, QtGui.QColor(0, 0, 0))
        if self._frame is not None and not self._frame.isNull():
            # Scale with aspect preserved; fill within rect
            scaled = self._frame.scaled(rect.size(), QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
            x = (rect.width() - scaled.width()) // 2
            y = (rect.height() - scaled.height()) // 2
            painter.drawImage(QtCore.QPoint(x, y), scaled)
        painter.end()


class ProjectorMirror(QtWidgets.QMainWindow):
    def __init__(self, fps: int = 30):
        super().__init__()
        self.setWindowTitle("Projector Mirror")
        self.setWindowIcon(QtGui.QIcon())

        # Determine primary screen geometry to establish source aspect ratio
        primary = QtWidgets.QApplication.primaryScreen()
        pgeo = primary.geometry()
        self.source_width = pgeo.width()
        self.source_height = pgeo.height()
        self.aspect_ratio = self.source_width / self.source_height

        self.viewer = AspectRatioWidget(self.aspect_ratio, self)
        self.setCentralWidget(self.viewer)

        # Capture setup
        self.sct = mss()
        # Identify the primary monitor index for mss; mss uses 1-based monitor indexing with 1 as "all"
        # We will find the monitor that matches the primary Qt screen geometry.
        self.monitor_index = self._find_primary_mss_monitor_index()

        # Timer for grabbing frames
        self.fps = max(10, min(120, int(fps)))
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.grab_and_update)
        self.timer.start(int(1000 / self.fps))

        self.paused = False
        self.always_on_top = False

        # Place window on secondary screen if present
        self._place_on_preferred_screen()

        # Nice default size
        self.resize(self.viewer.sizeHint())

    # ---------- Screen / Window placement helpers ----------
    def _place_on_preferred_screen(self):
        screens = get_screens()
        primary = QtWidgets.QApplication.primaryScreen()

        # Choose a non-primary (projector) if available, else primary
        target = None
        for s in screens:
            if s.name() != primary.name():
                target = s
                break
        if target is None:
            target = primary

        self._move_to_screen(target, center=True)

    def _move_to_screen(self, screen: QtGui.QScreen, center: bool = True):
        geo = screen.availableGeometry()
        if center:
            # Center a size-hinted window on that screen
            size = self.sizeHint()
            w = min(size.width(), geo.width())
            h = min(size.height(), geo.height())
            x = geo.x() + (geo.width() - w) // 2
            y = geo.y() + (geo.height() - h) // 2
            self.setGeometry(x, y, w, h)
        else:
            # Move top-left to the screen's top-left
            self.move(geo.topLeft())

    def move_to_next_monitor(self):
        screens = get_screens()
        if not screens:
            return
        # Find current screen index
        cur = self.windowHandle().screen()
        try:
            idx = screens.index(cur)
        except ValueError:
            idx = 0
        next_idx = (idx + 1) % len(screens)
        self._move_to_screen(screens[next_idx], center=True)

    def move_to_primary_or_secondary(self, secondary=False):
        screens = get_screens()
        primary = QtWidgets.QApplication.primaryScreen()
        if not secondary:
            self._move_to_screen(primary, center=True)
            return
        # Find a non-primary
        for s in screens:
            if s.name() != primary.name():
                self._move_to_screen(s, center=True)
                return
        # Fallback to primary
        self._move_to_screen(primary, center=True)

    # ---------- MSS primary monitor detection ----------
    def _find_primary_mss_monitor_index(self) -> int:
        # mss.monitors[0] is a virtual "full area"; real monitors start at 1
        # We'll match against the Qt primary screen geometry.
        primary = QtWidgets.QApplication.primaryScreen().geometry()
        for i, m in enumerate(self.sct.monitors[1:], start=1):
            if (m["left"] == primary.x() and
                m["top"] == primary.y() and
                m["width"] == primary.width() and
                m["height"] == primary.height()):
                return i
        # Fallback: use monitor 1 (likely primary)
        return 1

    # ---------- Capture & Update ----------
    @QtCore.Slot()
    def grab_and_update(self):
        if self.paused:
            return
        try:
            mon = self.sct.monitors[self.monitor_index]
            frame = self.sct.grab(mon)
            # Convert to QImage (BGRA → RGBA)
            img = np.asarray(frame)  # shape (h, w, 4), BGRA
            # Create QImage that references the numpy buffer (copy later when drawing)
            h, w, _ = img.shape
            qimg = QtGui.QImage(img.data, w, h, QtGui.QImage.Format.Format_BGRA8888)
            # Assign frame
            self.viewer.set_frame(qimg.copy())  # copy so buffer free is safe
        except Exception as e:
            # If something goes wrong, pause briefly
            print("Capture error:", e)
            time.sleep(0.05)

    # ---------- Window Flags ----------
    def toggle_fullscreen_borderless(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            # Borderless fullscreen on the current screen
            self.showFullScreen()

    def toggle_on_top(self):
        self.always_on_top = not self.always_on_top
        flags = self.windowFlags()
        if self.always_on_top:
            flags |= QtCore.Qt.WindowStaysOnTopHint
        else:
            flags &= ~QtCore.Qt.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()

    # ---------- Key handling ----------
    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
        key = event.key()
        if key in (QtCore.Qt.Key.Key_Q, QtCore.Qt.Key.Key_Escape):
            self.close()
            return
        if key == QtCore.Qt.Key.Key_F11:
            self.toggle_fullscreen_borderless()
            return
        if key == QtCore.Qt.Key.Key_Space:
            self.paused = not self.paused
            self.setWindowTitle(f"Projector Mirror ({'Paused' if self.paused else f'{self.fps} FPS'})")
            return
        if key == QtCore.Qt.Key.Key_P:
            self.toggle_on_top()
            return
        if key in (QtCore.Qt.Key.Key_Plus, QtCore.Qt.Key.Key_Equal):
            self.fps = min(120, self.fps + 5)
            self.timer.setInterval(int(1000 / self.fps))
            self.setWindowTitle(f"Projector Mirror ({self.fps} FPS)")
            return
        if key == QtCore.Qt.Key.Key_Minus:
            self.fps = max(10, self.fps - 5)
            self.timer.setInterval(int(1000 / self.fps))
            self.setWindowTitle(f"Projector Mirror ({self.fps} FPS)")
            return
        if key == QtCore.Qt.Key.Key_N:
            self.move_to_next_monitor()
            return
        if key == QtCore.Qt.Key.Key_1:
            self.move_to_primary_or_secondary(secondary=False)
            return
        if key == QtCore.Qt.Key.Key_2:
            self.move_to_primary_or_secondary(secondary=True)
            return
        super().keyPressEvent(event)


def main():
    app = QtWidgets.QApplication(sys.argv)
    w = ProjectorMirror(fps=30)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
