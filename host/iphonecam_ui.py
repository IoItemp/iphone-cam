#!/usr/bin/env python3
"""
iphonecam_ui.py - iPhoneCam with a control panel.

Same streaming as iphonecam.py (all its options work, e.g. --width 1280 --height 720
--fps 60 --rotate 180), plus a window with a live preview and camera controls:
focus (auto / lock / manual / click-to-focus), exposure, white balance, zoom,
torch and front/back camera. Settings are remembered and re-applied if the phone
reconnects. The phone app must be the updated version for the controls to work.
"""
import threading
import tkinter as tk
from tkinter import ttk

import cv2
from PIL import Image, ImageTk

import iphonecam as ic

SHUTTERS = [30, 50, 60, 100, 120, 250, 500, 1000, 2000]  # shutter = 1/x second
PREVIEW_MAX = (640, 480)


class App:
    def __init__(self, root: tk.Tk, args):
        self.root = root
        self.args = args
        self.ctrl = ic.Controller()
        self.stop = threading.Event()
        self.latest = None       # newest frame from the stream thread
        self.shown = None        # frame currently drawn
        self.status_msg = "Starting..."
        self.timers = {}         # slider debounce timers
        self.refreshers = []     # callbacks that redraw slider value labels
        self.view = (1, 1)       # size of the preview as drawn

        root.title("iPhoneCam")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.build()
        threading.Thread(target=self.stream, daemon=True).start()
        self.refresh()

    # ------------------------------------------------------------ layout ---

    def build(self) -> None:
        left = ttk.Frame(self.root)
        left.grid(row=0, column=0, padx=8, pady=8, sticky="n")
        self.photo = ImageTk.PhotoImage(Image.new("RGB", (640, 360), "black"))
        self.preview = tk.Label(left, image=self.photo, bd=0)
        self.preview.pack()
        self.preview.bind("<Button-1>", self.on_click)
        ttk.Label(left, text="Click the preview to focus on that spot").pack(pady=(4, 0))
        self.status = tk.StringVar(value=self.status_msg)
        ttk.Label(left, textvariable=self.status).pack(pady=(2, 0))

        right = ttk.Frame(self.root)
        right.grid(row=0, column=1, padx=(0, 8), pady=8, sticky="n")

        # Focus
        box = ttk.LabelFrame(right, text="Focus")
        box.pack(fill="x", pady=4)
        self.focus_mode = tk.StringVar(value="AUTO")
        self.radios(box, self.focus_mode, (("Auto", "AUTO"), ("Lock", "LOCK"), ("Manual", "MANUAL")),
                    self.send_focus)
        self.lens = tk.DoubleVar(value=0.5)
        self.lens_scale = self.slider(box, "Distance (near to far)", self.lens, 0, 1,
                                      lambda v: f"{v:.2f}", "focus", self.send_focus)

        # Exposure
        box = ttk.LabelFrame(right, text="Exposure")
        box.pack(fill="x", pady=4)
        self.exp_mode = tk.StringVar(value="AUTO")
        self.radios(box, self.exp_mode, (("Auto", "AUTO"), ("Manual", "MANUAL")), self.send_exposure)
        self.bias = tk.DoubleVar(value=0.0)
        self.bias_scale = self.slider(box, "Brightness (EV)", self.bias, -2, 2,
                                      lambda v: f"{v:+.1f}", "exposure", self.send_exposure)
        self.iso = tk.DoubleVar(value=200)
        self.iso_scale = self.slider(box, "ISO", self.iso, 25, 3200,
                                     lambda v: f"{v:.0f}", "exposure", self.send_exposure)
        self.shutter = tk.DoubleVar(value=2)  # index into SHUTTERS
        self.shutter_scale = self.slider(box, "Shutter", self.shutter, 0, len(SHUTTERS) - 1,
                                         lambda v: f"1/{SHUTTERS[int(round(v))]}", "exposure",
                                         self.send_exposure)

        # White balance
        box = ttk.LabelFrame(right, text="White balance")
        box.pack(fill="x", pady=4)
        self.wb_auto = tk.BooleanVar(value=True)
        ttk.Checkbutton(box, text="Auto", variable=self.wb_auto, command=self.send_wb).pack(anchor="w", padx=4)
        self.kelvin = tk.DoubleVar(value=5000)
        self.kelvin_scale = self.slider(box, "Temperature (K)", self.kelvin, 2500, 8000,
                                        lambda v: f"{v:.0f}", "wb", self.send_wb)

        # Zoom, torch, camera
        box = ttk.LabelFrame(right, text="Camera")
        box.pack(fill="x", pady=4)
        self.zoom = tk.DoubleVar(value=1.0)
        self.slider(box, "Zoom", self.zoom, 1, 10, lambda v: f"{v:.1f}x", "zoom",
                    lambda: self.ctrl.send(f"ZOOM {self.zoom.get():.2f}"))
        self.torch = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="Torch (back camera)", variable=self.torch,
                        command=lambda: self.ctrl.send(f"TORCH {int(self.torch.get())}")).pack(anchor="w", padx=4)
        self.cam = tk.StringVar(value="BACK")
        self.radios(box, self.cam, (("Back camera", "BACK"), ("Front camera", "FRONT")),
                    lambda: self.ctrl.send(f"CAMERA {self.cam.get()}"))

        ttk.Button(right, text="Reset all to auto", command=self.reset).pack(fill="x", pady=(8, 0))
        self.update_states()

    def radios(self, parent, var, options, command) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=4)
        for text, value in options:
            ttk.Radiobutton(row, text=text, value=value, variable=var,
                            command=command).pack(side="left", padx=(0, 8))

    def slider(self, parent, text, var, lo, hi, fmt, key, send):
        row = ttk.Frame(parent)
        row.pack(fill="x", padx=4, pady=2)
        head = ttk.Frame(row)
        head.pack(fill="x")
        ttk.Label(head, text=text).pack(side="left")
        value = ttk.Label(head, text=fmt(var.get()))
        value.pack(side="right")

        def redraw():
            value.config(text=fmt(var.get()))

        def changed(_):
            redraw()
            self.later(key, send)

        self.refreshers.append(redraw)
        scale = ttk.Scale(row, from_=lo, to=hi, variable=var, command=changed, length=260)
        scale.pack(fill="x")
        return scale

    # ---------------------------------------------------------- commands ---

    def later(self, key: str, fn) -> None:
        """Send at most one update per 50 ms while a slider is dragged (the latest value wins)."""
        if key in self.timers:
            return

        def run():
            self.timers.pop(key, None)
            fn()

        self.timers[key] = self.root.after(50, run)

    def send_focus(self) -> None:
        mode = self.focus_mode.get()
        if mode == "MANUAL":
            self.ctrl.send(f"FOCUS MANUAL {self.lens.get():.3f}")
        else:
            self.ctrl.send(f"FOCUS {mode}")
        self.update_states()

    def send_exposure(self) -> None:
        if self.exp_mode.get() == "MANUAL":
            den = SHUTTERS[int(round(self.shutter.get()))]
            self.ctrl.send(f"EXPOSURE MANUAL {self.iso.get():.0f} {den}")
        else:
            self.ctrl.send(f"EXPOSURE AUTO {self.bias.get():.2f}")
        self.update_states()

    def send_wb(self) -> None:
        if self.wb_auto.get():
            self.ctrl.send("WB AUTO")
        else:
            self.ctrl.send(f"WB KELVIN {self.kelvin.get():.0f}")
        self.update_states()

    def update_states(self) -> None:
        manual_focus = self.focus_mode.get() == "MANUAL"
        manual_exp = self.exp_mode.get() == "MANUAL"
        for widget, on in ((self.lens_scale, manual_focus),
                           (self.bias_scale, not manual_exp),
                           (self.iso_scale, manual_exp),
                           (self.shutter_scale, manual_exp),
                           (self.kelvin_scale, not self.wb_auto.get())):
            widget.state(["!disabled"] if on else ["disabled"])

    def reset(self) -> None:
        self.focus_mode.set("AUTO")
        self.exp_mode.set("AUTO")
        self.bias.set(0.0)
        self.wb_auto.set(True)
        self.zoom.set(1.0)
        self.torch.set(False)
        self.ctrl.reset()
        for redraw in self.refreshers:
            redraw()
        self.update_states()

    def on_click(self, event) -> None:
        """Tap to focus. The click is mapped back through the host's flip/rotation
        to the phone sensor's own coordinates."""
        w, h = self.view
        if w <= 1 or h <= 1:
            return
        u = min(max(event.x / w, 0.0), 1.0)
        v = min(max(event.y / h, 0.0), 1.0)
        if self.args.flip:
            u = 1 - u
        r = self.args.rotate
        if r == 90:
            u, v = v, 1 - u
        elif r == 180:
            u, v = 1 - u, 1 - v
        elif r == 270:
            u, v = 1 - v, u
        self.focus_mode.set("AUTO")
        self.ctrl.send(f"FOCUSPOINT {u:.4f} {v:.4f}")
        self.update_states()

    # ------------------------------------------------------------ stream ---

    def set_frame(self, frame) -> None:  # called from the stream thread
        self.latest = frame

    def set_status(self, msg: str) -> None:  # called from the stream thread
        self.status_msg = msg

    def stream(self) -> None:
        try:
            ic.run_forever(self.args, controller=self.ctrl, on_frame=self.set_frame,
                           stop_event=self.stop, on_status=self.set_status)
        except SystemExit as e:
            self.set_status(str(e.code))
        except Exception as e:
            self.set_status(f"Error: {e}")

    def refresh(self) -> None:
        frame = self.latest
        if frame is not None and frame is not self.shown:
            self.shown = frame
            h, w = frame.shape[:2]
            scale = min(PREVIEW_MAX[0] / w, PREVIEW_MAX[1] / h)
            size = (max(1, int(w * scale)), max(1, int(h * scale)))
            small = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
            self.photo = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB)))
            self.preview.configure(image=self.photo)
            self.view = size
        if self.status.get() != self.status_msg:
            self.status.set(self.status_msg)
        self.root.after(66, self.refresh)

    def close(self) -> None:
        self.stop.set()
        self.root.destroy()


def main() -> None:
    args = ic.build_parser().parse_args()
    args.preview = False  # this window is the preview
    try:  # crisp text on high-DPI Windows screens
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root = tk.Tk()
    App(root, args)
    root.mainloop()


if __name__ == "__main__":
    main()
