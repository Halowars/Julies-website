#!/usr/bin/env python3
"""
Projector Mirror (Simple v3 - Safe/Clamped)
- Same flow: pick projector -> black fullscreen window -> aspect-locked rectangle -> 'S' to mirror @ 60 FPS.
- Fix: clamps rectangle within window bounds during drag/resize to avoid OverflowErrors from extreme coords.
"""
from __future__ import annotations

import sys
import time
from typing import Optional

import numpy as np
from mss import mss

from PySide6 import QtCore, QtGui, QtWidgets


HANDLE_SIZE = 14  # px


def primary_aspect_ratio() -> float:
    scr = QtWidgets.QApplication.primaryScreen()
    g = scr.geometry()
    return g.width() / g.height()


class DragRectOverlay(QtWidgets.QWidget):
    def __init__(self, projector_screen: QtGui.QScreen, aspect: float, parent=None):
        super().__init__(parent)
        self.projector_screen = projector_screen
        self.aspect = max(0.1, float(aspect))  # guard

        self.setMouseTracking(True)
        self.setCursor(QtCore.Qt.ArrowCursor)
        self.setAutoFillBackground(True)
        pal = self.palette()
        pal.setColor(self.backgroundRole(), QtGui.QColor(0, 0, 0))
        self.setPalette(pal)

        # Place widget on projector screen
        self.setGeometry(self.projector_screen.geometry())
        self.setWindowTitle("Projector Mirror (Setup)")
        self.setWindowFlags(self.windowFlags() | QtCore.Qt.FramelessWindowHint)
        self.showFullScreen()

        # Init rect centered
        self._reset_rect_centered()

        # State
        self.dragging = False
        self.resizing = False
        self.drag_offset = QtCore.QPoint(0, 0)
        self.resize_anchor: Optional[str] = None

        # Mirror
        self.mirror_mode = False
        self.frame_img: Optional[QtGui.QImage] = None
        self.sct: Optional[mss] = None
        self.monitor_index = 1
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self._grab_frame)
        self.fps = 60

        self.setAttribute(QtCore.Qt.WA_OpaquePaintEvent, True)

    def _reset_rect_centered(self):
        w = max(1, self.width())
        h = max(1, self.height())
        target_h = max(200, h // 3)
        target_w = int(target_h * self.aspect)
        if target_w > w:
            target_w = max(50, w // 2)
            target_h = int(target_w / self.aspect)
        x = max(0, (w - target_w) // 2)
        y = max(0, (h - target_h) // 2)
        self.rect = QtCore.QRect(x, y, target_w, target_h)

    # ---------- Handles ----------
    def _handle_rects(self) -> dict:
        r = self.rect
        s = HANDLE_SIZE
        return {
            'tl': QtCore.QRect(r.left() - s//2, r.top() - s//2, s, s),
            'tr': QtCore.QRect(r.right() - s//2, r.top() - s//2, s, s),
            'bl': QtCore.QRect(r.left() - s//2, r.bottom() - s//2, s, s),
            'br': QtCore.QRect(r.right() - s//2, r.bottom() - s//2, s, s),
        }

    def _hit_test(self, pos: QtCore.QPoint) -> Optional[str]:
        for name, hr in self._handle_rects().items():
            if hr.contains(pos):
                return name
        if self.rect.contains(pos):
            return 'move'
        return None

    # ---------- Paint ----------
    def paintEvent(self, event: QtGui.QPaintEvent) -> None:
        p = QtGui.QPainter(self)
        if self.mirror_mode and self.frame_img is not None and not self.frame_img.isNull():
            scaled = self.frame_img.scaled(self.rect.size(), QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation)
            p.drawImage(self.rect.topLeft(), scaled)

        if not self.mirror_mode:
            pen = QtGui.QPen(QtGui.QColor(0, 180, 255), 2)
            p.setPen(pen)
            p.drawRect(self.rect)

            p.setBrush(QtGui.QBrush(QtGui.QColor(0, 180, 255)))
            for hr in self._handle_rects().values():
                p.drawRect(hr)

            p.setPen(QtGui.QPen(QtGui.QColor(220, 220, 220)))
            msg = "Drag corners to resize (aspect locked). Drag inside to move. Press 'S' to start. 'R' reset, F11 fullscreen, Esc quit."
            p.drawText(self.rect.adjusted(0, -24, 0, 0), QtCore.Qt.AlignBottom | QtCore.Qt.AlignHCenter, msg)
        p.end()

    # ---------- Mouse (local coords) ----------
    def mousePressEvent(self, e: QtGui.QMouseEvent) -> None:
        if e.button() != QtCore.Qt.LeftButton or self.mirror_mode:
            return
        pos = e.position().toPoint()
        hit = self._hit_test(pos)
        if hit == 'move':
            self.dragging = True
            self.drag_offset = pos - self.rect.topLeft()
        elif hit in ('tl', 'tr', 'bl', 'br'):
            self.resizing = True
            self.resize_anchor = hit
            self.drag_offset = pos
        e.accept()

    def mouseMoveEvent(self, e: QtGui.QMouseEvent) -> None:
        if self.mirror_mode:
            return
        pos = e.position().toPoint()
        hit = self._hit_test(pos)
        if hit in ('tl','br'):
            self.setCursor(QtCore.Qt.SizeFDiagCursor)
        elif hit in ('tr','bl'):
            self.setCursor(QtCore.Qt.SizeBDiagCursor)
        elif hit == 'move':
            self.setCursor(QtCore.Qt.SizeAllCursor)
        else:
            self.setCursor(QtCore.Qt.ArrowCursor)

        if self.dragging:
            new_top_left = pos - self.drag_offset
            self._move_rect_clamped(new_top_left)
            self.update()
        elif self.resizing and self.resize_anchor:
            self._resize_with_aspect_clamped(pos)
            self.update()

    def mouseReleaseEvent(self, e: QtGui.QMouseEvent) -> None:
        if e.button() != QtCore.Qt.LeftButton:
            return
        self.dragging = False
        self.resizing = False
        self.resize_anchor = None

    # ---------- Clamp helpers ----------
    def _move_rect_clamped(self, new_top_left: QtCore.QPoint):
        x = int(new_top_left.x())
        y = int(new_top_left.y())
        x = max(0, min(x, self.width() - self.rect.width()))
        y = max(0, min(y, self.height() - self.rect.height()))
        self.rect.moveTo(x, y)

    def _resize_with_aspect_clamped(self, pos: QtCore.QPoint):
        # Fixed corner in local coords
        r = self.rect
        fixed = {
            'tl': r.bottomRight(),
            'tr': r.bottomLeft(),
            'bl': r.topRight(),
            'br': r.topLeft(),
        }[self.resize_anchor]

        dx = pos.x() - fixed.x()
        dy = pos.y() - fixed.y()

        # determine sign
        sign_x = 1 if dx > 0 else -1
        sign_y = 1 if dy > 0 else -1

        # Candidate size from dx
        new_w = max(50, abs(dx))
        new_h = int(round(new_w / self.aspect))
        if abs(new_h) > max(50, abs(dy)):
            new_h = max(50, abs(dy))
            new_w = int(round(new_h * self.aspect))

        # Clamp size to window bounds
        max_w = self.width()
        max_h = self.height()
        new_w = max(50, min(new_w, max_w))
        new_h = max(50, min(new_h, max_h))

        # Build unclamped new rect from fixed corner
        if self.resize_anchor == 'tl':
            new_left = fixed.x() - sign_x * new_w
            new_top = fixed.y() - sign_y * new_h
        elif self.resize_anchor == 'tr':
            new_left = fixed.x() + sign_x * 0 - new_w + (sign_x * new_w)  # simplifies to fixed.x() - new_w if sign_x>0 else fixed.x()
            new_left = fixed.x() - new_w if sign_x > 0 else fixed.x()
            new_top = fixed.y() - sign_y * new_h
        elif self.resize_anchor == 'bl':
            new_left = fixed.x() - sign_x * new_w
            new_top = fixed.y() - new_h if sign_y > 0 else fixed.y()
        else:  # 'br'
            new_left = fixed.x() - new_w if sign_x > 0 else fixed.x()
            new_top = fixed.y() - new_h if sign_y > 0 else fixed.y()

        # Clamp position to keep rect within window
        new_left = int(max(0, min(new_left, self.width() - new_w)))
        new_top = int(max(0, min(new_top, self.height() - new_h)))

        try:
            self.rect = QtCore.QRect(QtCore.QPoint(new_left, new_top), QtCore.QSize(int(new_w), int(new_h)))
        except OverflowError:
            # If anything went weird, just ignore this frame
            pass

    # ---------- Keys ----------
    def keyPressEvent(self, e: QtGui.QKeyEvent) -> None:
        key = e.key()
        if key in (QtCore.Qt.Key.Key_Escape, QtCore.Qt.Key.Key_Q):
            self.close()
            return
        if key == QtCore.Qt.Key.Key_F11:
            if self.isFullScreen():
                self.showNormal()
            else:
                self.showFullScreen()
            return
        if key == QtCore.Qt.Key.Key_R and not self.mirror_mode:
            self._reset_rect_centered()
            self.update()
            return
        if key == QtCore.Qt.Key.Key_S and not self.mirror_mode:
            self.start_mirroring()
            return

    # ---------- Mirroring ----------
    def _detect_primary_mss_monitor(self) -> int:
        qt_primary = QtWidgets.QApplication.primaryScreen().geometry()
        with mss() as s:
            for i, m in enumerate(s.monitors[1:], start=1):
                if (m["left"] == qt_primary.x() and
                    m["top"] == qt_primary.y() and
                    m["width"] == qt_primary.width() and
                    m["height"] == qt_primary.height()):
                    return i
        return 1

    def start_mirroring(self):
        self.mirror_mode = True
        self.setWindowTitle("Projector Mirror")
        self.sct = mss()
        self.monitor_index = self._detect_primary_mss_monitor()
        self.timer.start(int(1000 / self.fps))

    @QtCore.Slot()
    def _grab_frame(self):
        if not self.mirror_mode or self.sct is None:
            return
        try:
            mon = self.sct.monitors[self.monitor_index]
            frame = self.sct.grab(mon)
            img = np.asarray(frame)  # BGRA
            h, w, _ = img.shape
            qimg = QtGui.QImage(img.data, w, h, QtGui.QImage.Format.Format_BGRA8888)
            self.frame_img = qimg.copy()
            self.update(self.rect)
        except Exception as e:
            print("Capture error:", e)
            time.sleep(0.01)


class ProjectorChooser(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Projector Display")
        layout = QtWidgets.QVBoxLayout(self)

        self.combo = QtWidgets.QComboBox(self)
        self.screens = QtWidgets.QApplication.screens()
        primary = QtWidgets.QApplication.primaryScreen()

        for idx, s in enumerate(self.screens):
            g = s.geometry()
            tag = " (PRIMARY)" if s == primary else ""
            self.combo.addItem(f"{idx}: {s.name()}  [{g.width()}x{g.height()} @ ({g.x()},{g.y()})]{tag}")

        layout.addWidget(QtWidgets.QLabel("Choose the display that your projector is connected to:"))
        layout.addWidget(self.combo)

        btns = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel, parent=self)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def selected_screen(self) -> Optional[QtGui.QScreen]:
        idx = self.combo.currentIndex()
        if 0 <= idx < len(self.screens):
            return self.screens[idx]
        return None


def main():
    app = QtWidgets.QApplication(sys.argv)

    dlg = ProjectorChooser()
    if dlg.exec() != QtWidgets.QDialog.Accepted:
        return
    proj_screen = dlg.selected_screen()
    if proj_screen is None:
        return

    aspect = primary_aspect_ratio()
    w = DragRectOverlay(projector_screen=proj_screen, aspect=aspect)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
