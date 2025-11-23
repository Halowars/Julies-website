#!/usr/bin/env python3
"""
Projector Mirror (Tkinter Edition)
==================================
- Prompts you to pick which display is the PROJECTOR (from mss monitors).
- Opens a BLACK borderless fullscreen window on that display.
- Shows a draggable, aspect-locked rectangle (matches PRIMARY screen aspect).
- Press 's' to save -> starts mirroring primary screen into that rectangle ~60 FPS.
- Press 'f' to toggle borderless/fullscreen; 'r' reset rectangle; 'q' or 'Esc' to quit.

Install:
    pip install mss pillow

Run:
    python projector_mirror_tk.py
"""
import sys
import time
import threading

import tkinter as tk
from tkinter import ttk, messagebox

from mss import mss
from PIL import Image, ImageTk
import numpy as np


HANDLE = 10  # corner handle radius (px)
MIN_W, MIN_H = 80, 45  # min rect size


def get_primary_aspect(sct):
    # mss.monitors[1:] are physical monitors; monitor[0] is virtual bounding box.
    # Primary is the one with (left, top) == (0, 0) most often, but better to use the biggest that contains origin.
    primary = None
    for m in sct.monitors[1:]:
        if m.get('left') == 0 and m.get('top') == 0:
            primary = m
            break
    if primary is None:
        primary = sct.monitors[1]
    return primary['width'] / primary['height'], primary


class ProjectorMirrorApp:
    def __init__(self):
        self.sct = mss()
        self.aspect, self.primary_m = get_primary_aspect(self.sct)

        # 1) Choose projector monitor
        self.projector_index = self.choose_projector_monitor()
        if self.projector_index is None:
            sys.exit(0)
        self.proj_m = self.sct.monitors[self.projector_index]

        # 2) Build the GUI on projector
        self.root = tk.Tk()
        self.root.configure(bg='black')
        self.root.attributes('-topmost', False)
        self.root.overrideredirect(True)  # borderless
        # Place on projector monitor
        W, H = self.proj_m['width'], self.proj_m['height']
        X, Y = self.proj_m['left'], self.proj_m['top']
        self.root.geometry(f"{W}x{H}+{X}+{Y}")
        # Fullscreen-like (borderless already)
        self.fullscreen = True

        # Canvas for drawing
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
        self.rect = [rx, ry, rx + rw, ry + rh]  # [x1, y1, x2, y2]

        # Draw initial rect + handles + instruction
        self.rect_id = None
        self.handle_ids = []
        self.frame_img_id = None
        self.tk_frame = None  # PhotoImage cache
        self.mirroring = False
        self.stop_flag = threading.Event()

        self.draw_setup_overlay()

        # Mouse interaction state
        self.dragging_mode = None  # 'move' or one of corners 'tl','tr','bl','br'
        self.prev_mouse = (0, 0)

        # Bindings
        self.canvas.bind('<Button-1>', self.on_mouse_down)
        self.canvas.bind('<B1-Motion>', self.on_mouse_drag)
        self.canvas.bind('<ButtonRelease-1>', self.on_mouse_up)
        self.root.bind('<KeyPress>', self.on_key)

        self.root.mainloop()

    # ---------- Monitor selection ----------
    def choose_projector_monitor(self):
        # Simple terminal prompt; list monitors
        mons = self.sct.monitors[1:]  # ignore [0]
        print("\nSelect your PROJECTOR display:")
        for idx, m in enumerate(mons, start=1):
            tag = " (PRIMARY?)" if (m.get('left') == 0 and m.get('top') == 0) else ""
            print(f"  {idx}: {m['width']}x{m['height']} at ({m['left']},{m['top']}){tag}")
        try:
            sel = input("Enter monitor number (or blank to cancel): ").strip()
        except EOFError:
            return None
        if not sel:
            return None
        try:
            i = int(sel)
        except ValueError:
            return None
        if 1 <= i <= len(mons):
            # mss global indices start at 1 for first physical; so actual index = i
            return i
        return None

    # ---------- Drawing helpers ----------
    def clear_overlay(self):
        if self.rect_id is not None:
            self.canvas.delete(self.rect_id)
            self.rect_id = None
        for hid in self.handle_ids:
            self.canvas.delete(hid)
        self.handle_ids = []
        # instruction text
        self.canvas.delete('instr')

    def draw_setup_overlay(self):
        self.clear_overlay()
        x1, y1, x2, y2 = map(int, self.rect)
        # rectangle
        self.rect_id = self.canvas.create_rectangle(x1, y1, x2, y2, outline='#00B4FF', width=2)
        # handles
        self.handle_ids = []
        for (hx, hy) in [(x1, y1), (x2, y1), (x1, y2), (x2, y2)]:
            self.handle_ids.append(self.canvas.create_rectangle(hx - HANDLE, hy - HANDLE, hx + HANDLE, hy + HANDLE,
                                                                outline='', fill='#00B4FF'))
        # instruction
        msg = "Drag corners to resize (aspect locked). Drag inside to move. Press 'S' to start mirroring. 'R' reset, 'F' fullscreen, Esc/Q quit."
        self.canvas.create_text((x1 + x2)//2, y1 - 20, text=msg, fill='#DDDDDD', tags='instr')

    # ---------- Mouse ----------
    def hit_test(self, x, y):
        x1, y1, x2, y2 = self.rect
        corners = {
            'tl': (x1, y1),
            'tr': (x2, y1),
            'bl': (x1, y2),
            'br': (x2, y2),
        }
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
        hit = self.hit_test(e.x, e.y)
        self.dragging_mode = hit

    def on_mouse_drag(self, e):
        if self.mirroring or not self.dragging_mode:
            return
        x, y = e.x, e.y
        W = self.proj_m['width']
        H = self.proj_m['height']
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
            # Resizing with aspect lock: compute size from distance to opposite corner
            if self.dragging_mode == 'tl':
                fx, fy = x2, y2
                sx, sy = x, y
                sign_x = -1
                sign_y = -1
            elif self.dragging_mode == 'tr':
                fx, fy = x1, y2
                sx, sy = x, y
                sign_x = 1
                sign_y = -1
            elif self.dragging_mode == 'bl':
                fx, fy = x2, y1
                sx, sy = x, y
                sign_x = -1
                sign_y = 1
            else:  # 'br'
                fx, fy = x1, y1
                sx, sy = x, y
                sign_x = 1
                sign_y = 1

            dx = abs(sx - fx)
            dy = abs(sy - fy)
            new_w = max(MIN_W, dx)
            new_h = int(round(new_w / self.aspect))
            if new_h > dy:
                new_h = max(MIN_H, dy)
                new_w = int(round(new_h * self.aspect))

            # Build new box from fixed corner
            if self.dragging_mode == 'tl':
                nx1 = max(0, min(fx - new_w, fx))
                ny1 = max(0, min(fy - new_h, fy))
                nx2, ny2 = fx, fy
            elif self.dragging_mode == 'tr':
                nx1, ny1 = fx, max(0, min(fy - new_h, fy))
                nx2 = min(W, max(fx + new_w, fx))
                ny2 = fy
            elif self.dragging_mode == 'bl':
                nx1 = max(0, min(fx - new_w, fx))
                ny1 = fy
                nx2, ny2 = fx, min(H, max(fy + new_h, fy))
            else:  # br
                nx1, ny1 = fx, fy
                nx2 = min(W, max(fx + new_w, fx))
                ny2 = min(H, max(fy + new_h, fy))

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
            # Toggle borderless fullscreen (borderless is already on)
            self.fullscreen = not self.fullscreen
            if self.fullscreen:
                self.root.overrideredirect(True)
            else:
                self.root.overrideredirect(False)
            return
        if k == 'r' and not self.mirroring:
            # Reset rect to centered
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
        # Remove handles/outline
        self.clear_overlay()
        # Create an image holder on canvas
        x1, y1, x2, y2 = map(int, self.rect)
        self.frame_img_id = self.canvas.create_image(x1, y1, anchor='nw')
        # Start capture thread + schedule UI updates
        t = threading.Thread(target=self.capture_loop, daemon=True)
        t.start()
        self.schedule_frame_draw()

    def capture_loop(self):
        # Grab primary monitor at ~60 FPS
        mon = self.primary_m
        interval = 1.0 / 60.0
        while not self.stop_flag.is_set():
            t0 = time.perf_counter()
            frame = self.sct.grab(mon)
            # Convert BGRA to RGB (PIL expects RGB)
            img = Image.frombytes('RGB', frame.size, frame.rgb)  # mss gives us .rgb already (no alpha)
            # Resize to our rect size
            x1, y1, x2, y2 = map(int, self.rect)
            w, h = max(1, x2 - x1), max(1, y2 - y1)
            img = img.resize((w, h), Image.BILINEAR)
            self.tk_frame = ImageTk.PhotoImage(img)
            # Try to keep 60 fps
            dt = time.perf_counter() - t0
            if dt < interval:
                time.sleep(interval - dt)

    def schedule_frame_draw(self):
        if self.stop_flag.is_set():
            return
        if self.mirroring and self.tk_frame is not None and self.frame_img_id is not None:
            # Update image position and content
            x1, y1, x2, y2 = map(int, self.rect)
            self.canvas.coords(self.frame_img_id, x1, y1)
            self.canvas.itemconfig(self.frame_img_id, image=self.tk_frame)
        self.root.after(10, self.schedule_frame_draw)  # ~100 fps UI refresh cap

if __name__ == '__main__':
    ProjectorMirrorApp()
