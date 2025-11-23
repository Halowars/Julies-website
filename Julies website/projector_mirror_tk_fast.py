#!/usr/bin/env python3
"""
Projector Mirror (Tk FAST - DXGI)
---------------------------------
Windows-optimized high-FPS mirror using DXGI Desktop Duplication via dxcam.

Features
- Pick SOURCE monitor to mirror and PROJECTOR monitor to display on.
- Black borderless fullscreen window on projector.
- Draggable, aspect-locked rectangle based on SOURCE aspect.
- Press 's' to save -> mirror SOURCE at target FPS (default 90; change with +/-).
- Uses dxcam (DirectX) for capture and OpenCV for very fast resizing.

Install:
    pip install dxcam opencv-python pillow
Run:
    python projector_mirror_tk_fast.py
"""
import sys
import time
import threading

import tkinter as tk
from PIL import Image, ImageTk
import numpy as np

try:
    import dxcam  # Windows-only
except Exception as e:
    print("dxcam not available. Install with: pip install dxcam")
    raise

# We'll still use mss just to enumerate monitor geometry consistently.
try:
    from mss import mss
except Exception:
    mss = None

import cv2  # for fast resize & color convert

HANDLE = 10
MIN_W, MIN_H = 80, 45


def list_monitors_mss():
    if mss is None:
        print("mss not installed. Install with: pip install mss")
        sys.exit(1)
    with mss() as s:
        return s.monitors[:]


def guess_primary_index(monitors):
    idx = None
    for i in range(1, len(monitors)):
        m = monitors[i]
        if m.get('left') == 0 and m.get('top') == 0:
            idx = i
            break
    if idx is None:
        areas = [(i, m['width'] * m['height']) for i, m in enumerate(monitors) if i != 0]
        idx = max(areas, key=lambda t: t[1])[0]
    return idx


class ProjectorMirrorFast:
    def __init__(self):
        mons = list_monitors_mss()
        if len(mons) < 2:
            print("No physical monitors detected by mss.")
            sys.exit(1)

        print("\nDetected monitors:")
        for i, m in enumerate(mons):
            tag = " (VIRTUAL)" if i == 0 else ""
            print(f"  {i}: {m['width']}x{m['height']} at ({m['left']},{m['top']}){tag}")
        primary_guess = guess_primary_index(mons)

        try:
            src_in = input(f"Enter SOURCE monitor to mirror [default {primary_guess}]: ").strip()
        except EOFError:
            src_in = ""
        self.source_idx = primary_guess if src_in == "" else int(src_in)

        try:
            proj_in = input("Enter PROJECTOR monitor index to display on (not 0): ").strip()
        except EOFError:
            print("Cancelled.")
            sys.exit(0)
        if proj_in == "" or int(proj_in) == 0:
            print("Need a physical monitor index for projector.")
            sys.exit(1)
        self.projector_idx = int(proj_in)

        self.monitors = mons
        self.source_m = self.monitors[self.source_idx]
        self.proj_m = self.monitors[self.projector_idx]

        self.aspect = self.source_m['width'] / self.source_m['height']

        # Tk window on projector
        self.root = tk.Tk()
        self.root.configure(bg='black')
        self.root.overrideredirect(True)
        self.fullscreen = True
        W, H = self.proj_m['width'], self.proj_m['height']
        X, Y = self.proj_m['left'], self.proj_m['top']
        self.root.geometry(f"{W}x{H}+{X}+{Y}")

        self.canvas = tk.Canvas(self.root, bg='black', highlightthickness=0, width=W, height=H)
        self.canvas.pack(fill='both', expand=True)

        # Initial rect
        rh = max(240, H // 3)
        rw = int(rh * self.aspect)
        if rw > W:
            rw = W // 2
            rh = int(rw / self.aspect)
        rx = (W - rw) // 2
        ry = (H - rh) // 2
        self.rect = [rx, ry, rx + rw, ry + rh]

        # Overlay + state
        self.rect_id = None
        self.handle_ids = []
        self.image_id = None
        self.tk_frame = None
        self._latest_bgr = None  # numpy array (BGR) produced by capture thread
        self._frame_lock = threading.Lock()

        self.mirroring = False
        self.stop_flag = threading.Event()
        self.target_fps = 90  # can bump to 120 on good GPUs/monitors

        self.draw_setup_overlay()

        # Mouse/keys
        self.dragging_mode = None
        self.prev_mouse = (0, 0)
        self.canvas.bind('<Button-1>', self.on_mouse_down)
        self.canvas.bind('<B1-Motion>', self.on_mouse_drag)
        self.canvas.bind('<ButtonRelease-1>', self.on_mouse_up)
        self.root.bind('<KeyPress>', self.on_key)

        # UI update loop
        self.root.after(5, self.ui_update_loop)

        self.root.mainloop()

    # ---------- Overlay helpers ----------
    def clear_overlay(self):
        if self.rect_id is not None:
            self.canvas.delete(self.rect_id)
            self.rect_id = None
        for hid in self.handle_ids:
            self.canvas.delete(hid)
        self.handle_ids = []
        self.canvas.delete('instr')

    def draw_setup_overlay(self):
        self.clear_overlay()
        x1, y1, x2, y2 = map(int, self.rect)
        self.rect_id = self.canvas.create_rectangle(x1, y1, x2, y2, outline='#00B4FF', width=2)
        for (hx, hy) in [(x1, y1), (x2, y1), (x1, y2), (x2, y2)]:
            self.handle_ids.append(self.canvas.create_rectangle(hx - HANDLE, hy - HANDLE, hx + HANDLE, hy + HANDLE,
                                                                outline='', fill='#00B4FF'))
        msg = "Drag corners to resize (locked). Drag inside to move. 'S' start, 'R' reset, 'F' border, +/- FPS, Esc/Q quit."
        self.canvas.create_text((x1 + x2)//2, max(20, y1 - 20), text=msg, fill='#DDDDDD', tags='instr')

    # ---------- Mouse ----------
    def hit_test(self, x, y):
        x1, y1, x2, y2 = self.rect
        corners = {'tl': (x1, y1), 'tr': (x2, y1), 'bl': (x1, y2), 'br': (x2, y2)}
        for name, (cx, cy) in corners.items():
            if abs(x - cx) <= HANDLE and abs(y - cy) <= HANDLE:
                return name
        if x1 < x < x2 and y1 < y < y2:
            return 'move'
        return None

    def on_mouse_down(self, e):
        if self.mirroring:
            return
        self.prev_mouse = (e.x, e.y)
        self.dragging_mode = self.hit_test(e.x, e.y)

    def on_mouse_drag(self, e):
        if self.mirroring or not self.dragging_mode:
            return
        x, y = e.x, e.y
        W, H = self.proj_m['width'], self.proj_m['height']
        x = max(0, min(x, W))
        y = max(0, min(y, H))
        x1, y1, x2, y2 = self.rect

        if self.dragging_mode == 'move':
            dx = x - self.prev_mouse[0]
            dy = y - self.prev_mouse[1]
            nx1 = max(0, min(x1 + dx, W - (x2 - x1)))
            ny1 = max(0, min(y1 + dy, H - (y2 - y1)))
            nx2 = nx1 + (x2 - x1)
            ny2 = ny1 + (y2 - y1)
            self.rect = [nx1, ny1, nx2, ny2]
        else:
            if self.dragging_mode == 'tl':
                fx, fy = x2, y2
            elif self.dragging_mode == 'tr':
                fx, fy = x1, y2
            elif self.dragging_mode == 'bl':
                fx, fy = x2, y1
            else:
                fx, fy = x1, y1
            dx = abs(x - fx)
            dy = abs(y - fy)
            new_w = max(MIN_W, dx)
            new_h = int(round(new_w / self.aspect))
            if new_h > dy:
                new_h = max(MIN_H, dy)
                new_w = int(round(new_h * self.aspect))

            if self.dragging_mode == 'tl':
                nx1, ny1 = max(0, fx - new_w), max(0, fy - new_h)
                nx2, ny2 = fx, fy
            elif self.dragging_mode == 'tr':
                nx1, ny1 = fx, max(0, fy - new_h)
                nx2, ny2 = min(W, fx + new_w), fy
            elif self.dragging_mode == 'bl':
                nx1, ny1 = max(0, fx - new_w), fy
                nx2, ny2 = fx, min(H, fy + new_h)
            else:
                nx1, ny1 = fx, fy
                nx2, ny2 = min(W, fx + new_w), min(H, fy + new_h)

            self.rect = [nx1, ny1, nx2, ny2]

        self.prev_mouse = (e.x, e.y)
        self.draw_setup_overlay()

    def on_mouse_up(self, e):
        self.dragging_mode = None

    # ---------- Keys ----------
    def on_key(self, e):
        k = e.keysym.lower()
        if k in ('escape', 'q'):
            self.stop_flag.set()
            self.root.destroy()
            return
        if k == 'f':
            self.fullscreen = not self.fullscreen
            self.root.overrideredirect(self.fullscreen)
            return
        if k == 'r' and not self.mirroring:
            W, H = self.proj_m['width'], self.proj_m['height']
            rh = max(240, H // 3)
            rw = int(rh * self.aspect)
            if rw > W:
                rw = W // 2
                rh = int(rw / self.aspect)
            rx = (W - rw) // 2
            ry = (H - rh) // 2
            self.rect = [rx, ry, rx + rw, ry + rh]
            self.draw_setup_overlay()
            return
        if k == 's' and not self.mirroring:
            self.start_mirroring()
            return
        if k in ('plus', 'equal'):
            self.target_fps = min(120, self.target_fps + 10)
            print(f"Target FPS: {self.target_fps}")
            return
        if k in ('minus', 'underscore'):
            self.target_fps = max(30, self.target_fps - 10)
            print(f"Target FPS: {self.target_fps}")
            return

    # ---------- Mirroring ----------
    def start_mirroring(self):
        self.mirroring = True
        self.clear_overlay()
        x1, y1, x2, y2 = map(int, self.rect)
        self.image_id = self.canvas.create_image(x1, y1, anchor='nw')
        t = threading.Thread(target=self.capture_loop_dx, daemon=True)
        t.start()

    def capture_loop_dx(self):
        # Capture source region with dxcam for high FPS
        cam = dxcam.create()  # default output mirrors the desktop; region defines capture area
        # Define region for the SOURCE monitor
        sm = self.source_m
        region = (sm['left'], sm['top'], sm['left'] + sm['width'], sm['top'] + sm['height'])

        interval = 1.0 / float(self.target_fps)
        last_fps_check = time.perf_counter()
        frames = 0
        while not self.stop_flag.is_set():
            t0 = time.perf_counter()
            frame = cam.grab(region=region)  # BGRA numpy array
            if frame is None:
                # Rarely happens on focus switches; skip
                time.sleep(0.005)
                continue

            # Convert BGRA -> BGR (drop alpha) then resize to rect size with OpenCV
            bgr = frame[:, :, :3]  # BGRA -> BGR
            # Determine current rect size atomically
            x1, y1, x2, y2 = map(int, self.rect)
            w, h = max(1, x2 - x1), max(1, y2 - y1)
            if (bgr.shape[1], bgr.shape[0]) != (w, h):
                bgr = cv2.resize(bgr, (w, h), interpolation=cv2.INTER_LINEAR)

            with self._frame_lock:
                self._latest_bgr = bgr

            frames += 1
            dt = time.perf_counter() - last_fps_check
            if dt >= 1.0:
                # Uncomment for debug:
                # print(f"Capture FPS ~ {frames/dt:.1f}")
                frames = 0
                last_fps_check = time.perf_counter()

            # pacing
            spent = time.perf_counter() - t0
            if spent < interval:
                time.sleep(max(0, interval - spent))

    def ui_update_loop(self):
        if self.stop_flag.is_set():
            return
        if self.mirroring and self.image_id is not None:
            with self._frame_lock:
                bgr = self._latest_bgr
                self._latest_bgr = None
            if bgr is not None:
                # Convert BGR -> RGB for Tk
                rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
                img = Image.fromarray(rgb)
                self.tk_frame = ImageTk.PhotoImage(img)
                x1, y1, x2, y2 = map(int, self.rect)
                self.canvas.coords(self.image_id, x1, y1)
                self.canvas.itemconfig(self.image_id, image=self.tk_frame)
        # UI loop fast enough to keep up
        self.root.after(4, self.ui_update_loop)  # ~250Hz loop

if __name__ == '__main__':
    ProjectorMirrorFast()
