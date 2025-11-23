#!/usr/bin/env python3
"""
Projector Mirror (Tk v2 - Robust)
---------------------------------
- Choose SOURCE (which monitor to mirror) and PROJECTOR (where to show window).
- Black, borderless fullscreen window on projector.
- Draggable, aspect-locked rectangle (based on SOURCE aspect).
- Press 's' to save -> mirror SOURCE into the rectangle at ~60 FPS.

Fixes:
- MSS is created inside the capture thread (Windows thread-local handles).
- PhotoImage is created on the Tk main thread (thread-safe).
- You can pick the SOURCE monitor explicitly (no guessing only).

Install:
    pip install mss pillow
Run:
    python projector_mirror_tk_v2.py
"""
import sys
import time
import threading

import tkinter as tk
from tkinter import messagebox

from mss import mss
from PIL import Image, ImageTk


HANDLE = 10
MIN_W, MIN_H = 80, 45


def list_monitors():
    with mss() as s:
        # s.monitors[0] is virtual bounding box; [1:] are real monitors
        return s.monitors[:]


def guess_primary_index(monitors):
    # Heuristic: primary often has (left, top) == (0, 0); otherwise pick the one with the largest area.
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


class ProjectorMirrorApp:
    def __init__(self):
        mons = list_monitors()
        if len(mons) < 2:
            print("No physical monitors detected by mss.")
            sys.exit(1)

        # Choose SOURCE and PROJECTOR via terminal prompt
        print("\nDetected monitors:")
        for i, m in enumerate(mons):
            if i == 0:
                print(f"  0: (VIRTUAL DESKTOP) {m['width']}x{m['height']} at ({m['left']},{m['top']})")
            else:
                print(f"  {i}: {m['width']}x{m['height']} at ({m['left']},{m['top']})")

        primary_guess = guess_primary_index(mons)
        try:
            src_in = input(f"Enter SOURCE monitor to mirror [default {primary_guess}]: ").strip()
        except EOFError:
            src_in = ""
        if src_in == "":
            self.source_idx = primary_guess
        else:
            self.source_idx = int(src_in)

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

        # Source aspect
        self.aspect = self.source_m['width'] / self.source_m['height']

        # Tk setup on projector
        self.root = tk.Tk()
        self.root.configure(bg='black')
        self.root.overrideredirect(True)  # borderless
        self.fullscreen = True

        W, H = self.proj_m['width'], self.proj_m['height']
        X, Y = self.proj_m['left'], self.proj_m['top']
        self.root.geometry(f"{W}x{H}+{X}+{Y}")

        self.canvas = tk.Canvas(self.root, bg='black', highlightthickness=0, width=W, height=H)
        self.canvas.pack(fill='both', expand=True)

        # Initial rect centered
        rh = max(200, H // 3)
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
        self._latest_img = None  # PIL.Image produced by capture thread
        self._frame_lock = threading.Lock()

        self.mirroring = False
        self.stop_flag = threading.Event()

        self.draw_setup_overlay()

        # Mouse/keys
        self.dragging_mode = None
        self.prev_mouse = (0, 0)
        self.canvas.bind('<Button-1>', self.on_mouse_down)
        self.canvas.bind('<B1-Motion>', self.on_mouse_drag)
        self.canvas.bind('<ButtonRelease-1>', self.on_mouse_up)
        self.root.bind('<KeyPress>', self.on_key)

        # UI update loop
        self.root.after(10, self.ui_update_loop)

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
        msg = "Drag corners to resize (locked). Drag inside to move. Press 'S' to start mirroring. 'R' reset, 'F' border, Esc/Q quit."
        self.canvas.create_text((x1 + x2)//2, y1 - 20, text=msg, fill='#DDDDDD', tags='instr')

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
            rh = max(200, H // 3)
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

    # ---------- Mirroring ----------
    def start_mirroring(self):
        self.mirroring = True
        self.clear_overlay()
        x1, y1, x2, y2 = map(int, self.rect)
        self.image_id = self.canvas.create_image(x1, y1, anchor='nw')
        t = threading.Thread(target=self.capture_loop, daemon=True)
        t.start()

    def capture_loop(self):
        # Create MSS in this thread (Windows requirement)
        with mss() as s:
            src_idx = self.source_idx
            interval = 1.0 / 120.0
            while not self.stop_flag.is_set():
                t0 = time.perf_counter()
                try:
                    mon = s.monitors[src_idx]
                except Exception:
                    # Fallback to virtual desktop if index invalid
                    mon = s.monitors[0]

                frame = s.grab(mon)
                # PIL image in RGB (no alpha)
                img = Image.frombytes('RGB', frame.size, frame.rgb)

                # Resize to rect size
                x1, y1, x2, y2 = map(int, self.rect)
                w, h = max(1, x2 - x1), max(1, y2 - y1)
                if img.size != (w, h):
                    img = img.resize((w, h), Image.BILINEAR)

                # Store latest frame for UI thread
                with self._frame_lock:
                    self._latest_img = img

                # pacing for ~60fps
                dt = time.perf_counter() - t0
                if dt < interval:
                    time.sleep(max(0, interval - dt))

    def ui_update_loop(self):
        # Called on Tk main thread
        if self.stop_flag.is_set():
            return
        if self.mirroring and self.image_id is not None:
            with self._frame_lock:
                img = self._latest_img
                self._latest_img = None
            if img is not None:
                self.tk_frame = ImageTk.PhotoImage(img)
                # Update image position & pixels
                x1, y1, x2, y2 = map(int, self.rect)
                self.canvas.coords(self.image_id, x1, y1)
                self.canvas.itemconfig(self.image_id, image=self.tk_frame)
        self.root.after(8, self.ui_update_loop)  # ~120 Hz UI loop

if __name__ == '__main__':
    ProjectorMirrorApp()
