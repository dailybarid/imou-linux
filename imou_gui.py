#!/usr/bin/env python3
"""imou_gui.py - control centre for an Imou/Lechange/Dahua-chipset camera.

Live view, snapshots, image + night-mode tuning, motion detection, SD-card
browser and local recording control. Everything runs over the local network --
no Imou cloud, no Windows, no vendor app.

Requires: python3-tk, python3-pil, python3-numpy, ffmpeg.
"""
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import numpy as np
from PIL import Image, ImageTk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from imou_sd import Dvrip, human  # noqa: E402

# ---------------- config ----------------
HOST = os.environ.get("IMOU_HOST", "192.168.1.100")
USER = os.environ.get("IMOU_USER", "admin")
PASS = os.environ.get("IMOU_PASS", "")
SUBTYPE = int(os.environ.get("IMOU_SUBTYPE", "0"))
OUTDIR = os.environ.get("IMOU_OUTDIR", os.path.expanduser("~/imou-recordings"))
SNAPDIR = os.path.expanduser("~/Pictures/imou")
FFMPEG = (os.environ.get("FFMPEG") or shutil.which("ffmpeg")
          or "/usr/bin/ffmpeg")
# NOTE: never build these with str.replace() -- the ffmpeg install directory is
# itself named "ffmpeg-9.0.1-linux-x64", so replace() corrupts the path.
_BINDIR = os.path.dirname(FFMPEG)
FFPLAY = os.path.join(_BINDIR, "ffplay")
FFPROBE = os.path.join(_BINDIR, "ffprobe")

ENV_FILE = os.path.expanduser("~/.config/imou-camera.env")
SETTINGS_FILE = os.path.expanduser("~/.config/imou-camera/settings.json")
AUTOSTART_FILE = os.path.expanduser("~/.config/autostart/imou-camera.desktop")
LAUNCHER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "launch.sh")
LOG_FILE = os.path.expanduser("~/.cache/imou-camera.log")

DEFAULT_SETTINGS = {"autostart_preview": True, "preview_stream": "Main 1080p"}


def read_env(path=ENV_FILE):
    """Parse KEY=value lines from the credential/env file."""
    d = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return d


def write_env(updates, path=ENV_FILE):
    """Rewrite the env file atomically: preserve unmanaged keys, stay mode 600."""
    d = read_env(path)
    d.update({k: str(v) for k, v in updates.items()})
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for k, v in d.items():
            f.write(f"{k}={v}\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            s.update(json.load(f))
    except Exception:
        pass
    return s


def save_settings(s):
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=1)
    os.replace(tmp, SETTINGS_FILE)


PREVIEW_W, PREVIEW_H, PREVIEW_FPS = 880, 495, 15

BG = "#0f1116"        # app background
BG2 = "#161a21"       # card surface
BG3 = "#1d222b"       # raised surface (inputs, popups)
BORDER = "#272d38"
FG = "#e7eaf0"
MUTED = "#8a93a6"
ACCENT = "#4d8dff"
ACCENT_DIM = "#1d2b45"
OK = "#35c97a"
BAD = "#f0575f"
WARN = "#f5a524"
FONT = "DejaVu Sans"


def rtsp_url(subtype=0):
    return (f"rtsp://{USER}:{PASS}@{HOST}:554/cam/realmonitor"
            f"?channel=1&subtype={subtype}&unicast=true&proto=Onvif")


# ---------------- camera backend ----------------
class Camera:
    """Thread-safe-ish wrapper: each call opens its own DVRIP session."""

    def __init__(self, host, user, password):
        self.host, self.user, self.password = host, user, password
        self.info = {}

    def _sess(self):
        c = Dvrip(self.host, self.user, self.password)
        c.login()
        return c

    def call(self, method, params=None):
        c = self._sess()
        try:
            return c.rpc(method, params)
        finally:
            c.close()

    def get_config(self, name):
        r = self.call("configManager.getConfig", {"name": name})
        if not r.get("result"):
            return None
        return (r.get("params") or {}).get("table")

    def set_config(self, name, table):
        return self.call("configManager.setConfig", {"name": name, "table": table})

    def device_type(self):
        return self.call("magicBox.getDeviceType").get("params", {}).get("type", "?")

    def storage(self):
        r = self.call("storage.getDeviceAllInfo")
        info = (r.get("params") or {}).get("info") or []
        if not info:
            return None
        d = (info[0].get("Detail") or [{}])[0]
        return {"path": d.get("Path"), "total": d.get("TotalBytes", 0),
                "used": d.get("UsedBytes", 0), "state": info[0].get("State")}

    def list_recordings(self, days=2, limit=None):
        c = self._sess()
        try:
            import datetime
            tok = c.rpc("mediaFileFind.factory.create").get("result")
            if not tok:
                return []
            end = datetime.datetime.now()
            start = end - datetime.timedelta(days=days)
            c.rpc("mediaFileFind.findFile", {"token": tok, "condition": {
                "Channel": 0,
                "StartTime": start.strftime("%Y-%m-%d %H:%M:%S"),
                "EndTime": end.strftime("%Y-%m-%d %H:%M:%S"),
                "Types": ["dav"], "Flags": ["Timing", "Event", "Manual"],
                "StreamType": 0}})
            files = []
            while True:
                rn = c.rpc("mediaFileFind.findNextFile", {"token": tok, "count": 100})
                infos = (rn.get("params") or {}).get("infos") or []
                if not infos:
                    break
                files.extend(infos)
                if len(infos) < 100:
                    break
            try:
                c.rpc("mediaFileFind.close", {"token": tok})
                c.rpc("mediaFileFind.destroy", {"token": tok})
            except Exception:
                pass
            return files[-limit:] if limit else files
        finally:
            c.close()


CAM = Camera(HOST, USER, PASS)


# ---------------- video preview ----------------
class Preview:
    def __init__(self, widget, frame_q):
        self.widget = widget
        self.q = frame_q
        self.img_id = None

    def pump(self):
        latest = None
        try:
            while True:
                latest = self.q.get_nowait()
        except queue.Empty:
            pass
        if latest is not None:
            img = ImageTk.PhotoImage(Image.fromarray(latest))
            if self.img_id is None:
                self.img_id = self.widget.create_image(
                    PREVIEW_W // 2, PREVIEW_H // 2, image=img)
            else:
                self.widget.itemconfig(self.img_id, image=img)
            self.widget.image = img


class VideoThread(threading.Thread):
    def __init__(self, url, q):
        super().__init__(daemon=True)
        self.url, self.q, self.stop = url, q, threading.Event()
        self.proc = None
        self.error = None

    def run(self):
        n = PREVIEW_W * PREVIEW_H * 3
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
               "-rtsp_transport", "tcp", "-timeout", "15000000",
               "-i", self.url, "-an",
               "-vf", (f"scale={PREVIEW_W}:{PREVIEW_H}:force_original_aspect_ratio=decrease,"
                       f"pad={PREVIEW_W}:{PREVIEW_H}:(ow-iw)/2:(oh-ih)/2"),
               "-r", str(PREVIEW_FPS), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                         stderr=subprocess.PIPE, bufsize=n * 2)
        except Exception as e:
            self.error = str(e)
            return
        while not self.stop.is_set():
            buf = self.proc.stdout.read(n)
            if not buf or len(buf) < n:
                break
            arr = np.frombuffer(buf, np.uint8).reshape(PREVIEW_H, PREVIEW_W, 3)
            try:
                self.q.put_nowait(arr.copy())
            except queue.Full:
                pass
        try:
            self.proc.kill()
        except Exception:
            pass

    def halt(self):
        self.stop.set()
        if self.proc:
            try:
                self.proc.kill()
            except Exception:
                pass


# ---------------- application ----------------
class App(tk.Tk):
    def __init__(self):
        # className sets WM_CLASS so the taskbar groups this window correctly
        # and StartupWMClass in the .desktop file matches it.
        super().__init__(className="ImouCamera")
        self.title("Imou Camera Control")
        self._set_icon()
        self.geometry("1490x845")
        self.minsize(1240, 700)
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.frame_q = queue.Queue(maxsize=3)
        self.video = None
        self.vc_tbl = None
        self.vio_tbl = None
        self.md_tbl = None
        self.settings = load_settings()
        self._build_options()
        self._build_style()
        self._build_ui()
        self.after(200, self._load_state)
        self.after(900, self._autostart)
        self.after(30, self._pump)

    # ---------- theme ----------
    def _set_icon(self):
        """Window / taskbar icon, generated by make_icon.py."""
        here = os.path.dirname(os.path.abspath(__file__))
        for name in ("icon-128.png", "icon.png"):
            p = os.path.join(here, name)
            if os.path.exists(p):
                try:
                    self._icon_img = ImageTk.PhotoImage(Image.open(p))
                    self.iconphoto(True, self._icon_img)
                    return
                except Exception:
                    pass

    def _build_options(self):
        """Tk option database. ttk styling does NOT reach combobox popup lists --
        without these the dropdown renders text on an unthemed (invisible) background."""
        o = self.option_add
        o("*TCombobox*Listbox.background", BG3)
        o("*TCombobox*Listbox.foreground", FG)
        o("*TCombobox*Listbox.selectBackground", ACCENT)
        o("*TCombobox*Listbox.selectForeground", "#ffffff")
        o("*TCombobox*Listbox.font", (FONT, 10))
        o("*TCombobox*Listbox.borderWidth", 0)
        o("*TCombobox*Listbox.activestyle", "none")
        o("*Menu.background", BG2)
        o("*Menu.foreground", FG)

    def _build_style(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure(".", background=BG, foreground=FG, font=(FONT, 10),
                     fieldbackground=BG3, bordercolor=BORDER,
                     lightcolor=BG3, darkcolor=BG3, focuscolor=ACCENT)

        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=BG2, borderwidth=0)
        st.configure("Panel.TFrame", background=BG2, relief="solid", borderwidth=1,
                     bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        st.configure("TLabel", background=BG, foreground=FG)
        st.configure("Card.TLabel", background=BG2, foreground=FG)
        st.configure("Muted.TLabel", background=BG2, foreground=MUTED, font=(FONT, 9))
        st.configure("H1.TLabel", background=BG, foreground=FG, font=(FONT, 16, "bold"))

        st.configure("TButton", background=BG3, foreground=FG, borderwidth=0,
                     focusthickness=0, padding=(13, 7), font=(FONT, 10))
        st.map("TButton",
               background=[("pressed", ACCENT_DIM), ("active", "#2a3140")],
               foreground=[("disabled", MUTED)])
        st.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                     font=(FONT, 10, "bold"))
        st.map("Accent.TButton",
               background=[("pressed", "#3b78e0"), ("active", "#6aa1ff")])
        st.configure("Danger.TButton", background="#3a2024", foreground="#ff9aa0")
        st.map("Danger.TButton", background=[("active", "#4d262b")])

        st.configure("TCheckbutton", background=BG2, foreground=FG,
                     focuscolor=BG2, padding=3)
        st.map("TCheckbutton", background=[("active", BG2)],
               indicatorcolor=[("selected", ACCENT), ("!selected", BG3)])

        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", background=BG, foreground=MUTED,
                     padding=(14, 9), borderwidth=0, font=(FONT, 10))
        st.map("TNotebook.Tab",
               background=[("selected", BG2)],
               foreground=[("selected", FG)])

        st.configure("TScale", background=ACCENT, troughcolor=BG3, borderwidth=0,
                     lightcolor=ACCENT, darkcolor=ACCENT, sliderlength=16)
        st.map("TScale", background=[("active", "#6aa1ff")])

        st.configure("TCombobox", fieldbackground=BG3, background=BG3,
                     foreground=FG, arrowcolor=MUTED, borderwidth=1,
                     bordercolor=BORDER, lightcolor=BG3, darkcolor=BG3,
                     selectbackground=BG3, selectforeground=FG, padding=(8, 5))
        st.map("TCombobox",
               fieldbackground=[("readonly", BG3), ("disabled", BG2)],
               foreground=[("readonly", FG), ("disabled", MUTED)],
               selectbackground=[("readonly", BG3)],
               selectforeground=[("readonly", FG)],
               arrowcolor=[("active", FG)])

        st.configure("Treeview", background=BG2, fieldbackground=BG2, foreground=FG,
                     borderwidth=0, rowheight=25, font=(FONT, 9))
        st.configure("Treeview.Heading", background=BG3, foreground=MUTED,
                     borderwidth=0, relief="flat", font=(FONT, 9, "bold"))
        st.map("Treeview.Heading", background=[("active", BG3)])
        st.map("Treeview", background=[("selected", ACCENT_DIM)],
               foreground=[("selected", FG)])

        st.configure("Horizontal.TProgressbar", background=ACCENT,
                     troughcolor=BG3, borderwidth=0, thickness=8)

    def _build_ui(self):
        # header
        head = ttk.Frame(self, padding=(16, 12, 16, 6))
        head.pack(fill="x")
        ttk.Label(head, text="Imou Camera Control", style="H1.TLabel").pack(side="left")
        self.lbl_dev = ttk.Label(head, text="connecting…", foreground=MUTED)
        self.lbl_dev.pack(side="left", padx=16)
        self.led = tk.Canvas(head, width=12, height=12, bg=BG, highlightthickness=0)
        self.led.pack(side="right", padx=(6, 0))
        self.led_dot = self.led.create_oval(2, 2, 11, 11, fill=WARN, outline="")
        self.lbl_status = ttk.Label(head, text="", foreground=MUTED)
        self.lbl_status.pack(side="right")

        body = ttk.Frame(self, padding=(16, 6, 16, 12))
        body.pack(fill="both", expand=True)

        # ---- left: video ----
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)

        self.canvas = tk.Canvas(left, width=PREVIEW_W, height=PREVIEW_H,
                                bg="#0a0c10", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.preview = Preview(self.canvas, self.frame_q)
        self.canvas.create_text(PREVIEW_W // 2, PREVIEW_H // 2, text="no signal",
                                fill="#333a45", font=(FONT, 20), tags="hint")

        bar = ttk.Frame(left, padding=(0, 10, 0, 0))
        bar.pack(fill="x")
        self.btn_play = ttk.Button(bar, text="Play", style="Accent.TButton",
                                   width=8, command=self.toggle_video)
        self.btn_play.pack(side="left")
        ttk.Button(bar, text="Snapshot", command=self.snapshot).pack(side="left", padx=6)
        ttk.Button(bar, text="Open HD player", command=self.open_hd).pack(side="left")

        self.stream_var = tk.StringVar(
            value=self.settings.get("preview_stream", "Main 1080p"))
        cb = ttk.Combobox(bar, textvariable=self.stream_var, width=12, state="readonly",
                          values=["Main 1080p", "Sub 640x480"])
        cb.pack(side="left", padx=12)
        cb.bind("<<ComboboxSelected>>",
                lambda e: (self.restart_video(), self.save_prefs()))

        self.lbl_fps = ttk.Label(bar, text="", foreground=MUTED)
        self.lbl_fps.pack(side="right")

        # ---- right: tabs ----
        side = ttk.Frame(body, width=555)
        side.pack(side="right", fill="y", padx=(16, 0))
        side.pack_propagate(False)
        nb = ttk.Notebook(side)
        nb.pack(fill="both", expand=True)
        self.nb = nb
        self.tab_image(nb)
        self.tab_night(nb)
        self.tab_motion(nb)
        self.tab_storage(nb)
        self.tab_record(nb)
        self.tab_device(nb)
        self.tab_settings(nb)

    def _card(self, parent, title, pad=16):
        f = ttk.Frame(parent, style="Panel.TFrame", padding=pad)
        ttk.Label(f, text=title, style="Card.TLabel",
                  font=(FONT, 10, "bold")).pack(anchor="w", pady=(0, 12))
        return f

    def _slider(self, parent, label, key, lo=0, hi=100):
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x", pady=3)
        ttk.Label(row, text=label, style="Card.TLabel", width=16).pack(side="left")
        var = tk.IntVar(value=50)
        val = ttk.Label(row, text="50", style="Muted.TLabel", width=4)
        val.pack(side="right")
        s = ttk.Scale(row, from_=lo, to=hi, orient="horizontal", variable=var)
        s.pack(side="left", fill="x", expand=True, padx=6)
        var.trace_add("write", lambda *_: val.config(text=str(int(var.get()))))
        return key, var

    # ---------- tabs ----------
    def tab_image(self, nb):
        wrap = ttk.Frame(nb, padding=8)
        nb.add(wrap, text="Image")
        card = self._card(wrap, "Picture")
        card.pack(fill="x")
        self.img_sliders = []
        for lbl, key in [("Brightness", "Brightness"), ("Contrast", "Contrast"),
                         ("Saturation", "Saturation"), ("Hue", "Hue"),
                         ("Gamma", "Gamma"), ("Chroma suppress", "ChromaSuppress")]:
            self.img_sliders.append(self._slider(card, lbl, key))

        row = ttk.Frame(card, style="Card.TFrame")
        row.pack(fill="x", pady=(10, 0))
        ttk.Label(row, text="Style", style="Card.TLabel", width=16).pack(side="left")
        self.style_var = tk.StringVar(value="Standard")
        ttk.Combobox(row, textvariable=self.style_var, width=14, state="readonly",
                     values=["Standard", "Soft", "Vivid"]).pack(side="left")

        btns = ttk.Frame(card, style="Card.TFrame")
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="Apply", style="Accent.TButton",
                   command=self.apply_image).pack(side="left")
        ttk.Button(btns, text="Reload", command=self.reload_image).pack(side="left", padx=6)

    def tab_night(self, nb):
        wrap = ttk.Frame(nb, padding=8)
        nb.add(wrap, text="Night")
        card = self._card(wrap, "Day / night")
        card.pack(fill="x")

        r1 = ttk.Frame(card, style="Card.TFrame"); r1.pack(fill="x", pady=3)
        ttk.Label(r1, text="Mode", style="Card.TLabel", width=16).pack(side="left")
        self.dnc_var = tk.StringVar(value="Auto")
        ttk.Combobox(r1, textvariable=self.dnc_var, width=14, state="readonly",
                     values=["Auto", "Color", "Black & white"]).pack(side="left")

        r2 = ttk.Frame(card, style="Card.TFrame"); r2.pack(fill="x", pady=3)
        ttk.Label(r2, text="Switch sensitivity", style="Card.TLabel",
                  width=16).pack(side="left")
        self.dns_var = tk.IntVar(value=2)
        ttk.Scale(r2, from_=0, to=6, orient="horizontal",
                  variable=self.dns_var).pack(side="left", fill="x", expand=True, padx=6)

        r3 = ttk.Frame(card, style="Card.TFrame"); r3.pack(fill="x", pady=3)
        ttk.Label(r3, text="White balance", style="Card.TLabel", width=16).pack(side="left")
        self.wb_var = tk.StringVar(value="Auto")
        ttk.Combobox(r3, textvariable=self.wb_var, width=14, state="readonly",
                     values=["Auto", "Manual", "Indoor", "Outdoor"]).pack(side="left")

        r4 = ttk.Frame(card, style="Card.TFrame"); r4.pack(fill="x", pady=3)
        ttk.Label(r4, text="Anti-flicker", style="Card.TLabel", width=16).pack(side="left")
        self.af_var = tk.StringVar(value="Off")
        ttk.Combobox(r4, textvariable=self.af_var, width=14, state="readonly",
                     values=["Off", "50 Hz", "60 Hz"]).pack(side="left")

        r5 = ttk.Frame(card, style="Card.TFrame"); r5.pack(fill="x", pady=(8, 0))
        self.ir_var = tk.BooleanVar(value=False)
        self.mirror_var = tk.BooleanVar(value=False)
        self.flip_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(r5, text="IR LED", variable=self.ir_var).pack(side="left")
        ttk.Checkbutton(r5, text="Mirror", variable=self.mirror_var).pack(side="left", padx=10)
        ttk.Checkbutton(r5, text="Flip", variable=self.flip_var).pack(side="left")

        btns = ttk.Frame(card, style="Card.TFrame")
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="Apply", style="Accent.TButton",
                   command=self.apply_night).pack(side="left")
        ttk.Button(btns, text="Reload", command=self.reload_night).pack(side="left", padx=6)

        note = ttk.Label(wrap, style="Muted.TLabel", wraplength=330, justify="left",
                         text="Camera is a fixed-lens model — pan/tilt/zoom is not "
                              "supported by its hardware, despite the ONVIF PTZ service "
                              "being advertised.")
        note.pack(anchor="w", padx=6, pady=10)

    def tab_motion(self, nb):
        wrap = ttk.Frame(nb, padding=8)
        nb.add(wrap, text="Motion")
        card = self._card(wrap, "Detection")
        card.pack(fill="x")
        self.md_enable = tk.BooleanVar(value=False)
        ttk.Checkbutton(card, text="Enable motion detection",
                        variable=self.md_enable).pack(anchor="w")

        r = ttk.Frame(card, style="Card.TFrame"); r.pack(fill="x", pady=(10, 0))
        ttk.Label(r, text="Sensitivity", style="Card.TLabel", width=16).pack(side="left")
        self.md_level = tk.IntVar(value=3)
        self.md_level_lbl = ttk.Label(r, text="3", style="Muted.TLabel", width=3)
        self.md_level_lbl.pack(side="right")
        s = ttk.Scale(r, from_=1, to=6, orient="horizontal", variable=self.md_level)
        s.pack(side="left", fill="x", expand=True, padx=6)
        self.md_level.trace_add("write", lambda *_: self.md_level_lbl.config(
            text=str(int(self.md_level.get()))))

        btns = ttk.Frame(card, style="Card.TFrame")
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="Apply", style="Accent.TButton",
                   command=self.apply_motion).pack(side="left")
        ttk.Button(btns, text="Reload", command=self.reload_motion).pack(side="left", padx=6)

        self.md_state = ttk.Label(wrap, text="", style="Muted.TLabel")
        self.md_state.pack(anchor="w", padx=6, pady=10)

    def tab_storage(self, nb):
        wrap = ttk.Frame(nb, padding=8)
        nb.add(wrap, text="SD")
        card = self._card(wrap, "Card usage")
        card.pack(fill="x")
        self.sd_bar = ttk.Progressbar(card, maximum=100, value=0)
        self.sd_bar.pack(fill="x")
        self.sd_text = ttk.Label(card, text="—", style="Muted.TLabel")
        self.sd_text.pack(anchor="w", pady=(6, 0))

        row = ttk.Frame(card, style="Card.TFrame")
        row.pack(fill="x", pady=(10, 0))
        ttk.Label(row, text="Show last", style="Card.TLabel").pack(side="left")
        self.days_var = tk.StringVar(value="1")
        ttk.Combobox(row, textvariable=self.days_var, width=5, state="readonly",
                     values=["1", "2", "7", "14"]).pack(side="left", padx=6)
        ttk.Label(row, text="day(s)", style="Card.TLabel").pack(side="left")
        ttk.Button(row, text="Refresh", command=self.load_storage).pack(side="right")

        card2 = self._card(wrap, "Recordings")
        card2.pack(fill="both", expand=True, pady=(10, 0))
        cols = ("start", "end", "dur", "size", "flags")
        tv = ttk.Treeview(card2, columns=cols, show="headings", height=14)
        for c, w, t in [("start", 130, "Start"), ("end", 80, "End"),
                        ("dur", 50, "Dur"), ("size", 70, "Size"),
                        ("flags", 90, "Type")]:
            tv.heading(c, text=t)
            tv.column(c, width=w, anchor="w")
        sb = ttk.Scrollbar(card2, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.sd_tree = tv
        self.sd_summary = ttk.Label(wrap, text="", style="Muted.TLabel")
        self.sd_summary.pack(anchor="w", padx=6, pady=(8, 0))

    def tab_record(self, nb):
        wrap = ttk.Frame(nb, padding=8)
        nb.add(wrap, text="Record")
        card = self._card(wrap, "Local recorder (this PC)")
        card.pack(fill="x")
        self.rec_state = ttk.Label(card, text="checking…", style="Card.TLabel",
                                   font=(FONT, 11, "bold"))
        self.rec_state.pack(anchor="w")
        self.rec_info = ttk.Label(card, text="", style="Muted.TLabel", wraplength=330,
                                  justify="left")
        self.rec_info.pack(anchor="w", pady=(6, 0))

        btns = ttk.Frame(card, style="Card.TFrame")
        btns.pack(fill="x", pady=(12, 0))
        ttk.Button(btns, text="Start", style="Accent.TButton",
                   command=lambda: self.svc("start")).pack(side="left")
        ttk.Button(btns, text="Stop", style="Danger.TButton",
                   command=lambda: self.svc("stop")).pack(side="left", padx=6)
        ttk.Button(btns, text="Restart", command=lambda: self.svc("restart")).pack(side="left")
        ttk.Button(btns, text="Open folder",
                   command=lambda: subprocess.Popen(["xdg-open", OUTDIR])).pack(side="left", padx=6)
        ttk.Button(btns, text="Refresh", command=self.refresh_rec).pack(side="right")

        card2 = self._card(wrap, "Recorded files")
        card2.pack(fill="both", expand=True, pady=(10, 0))
        tv = ttk.Treeview(card2, columns=("file", "size"), show="headings", height=12)
        tv.heading("file", text="File")
        tv.heading("size", text="Size")
        tv.column("file", width=250)
        tv.column("size", width=70, anchor="e")
        tv.pack(fill="both", expand=True)
        self.rec_tree = tv

    def tab_device(self, nb):
        wrap = ttk.Frame(nb, padding=8)
        nb.add(wrap, text="Device")
        card = self._card(wrap, "Camera")
        card.pack(fill="x")
        self.dev_text = ttk.Label(card, text="—", style="Card.TLabel", justify="left")
        self.dev_text.pack(anchor="w")

        card2 = self._card(wrap, "Stream URLs")
        card2.pack(fill="x", pady=(10, 0))
        for label, sub in [("Main", 0), ("Sub", 1)]:
            r = ttk.Frame(card2, style="Card.TFrame")
            r.pack(fill="x", pady=2)
            ttk.Label(r, text=label, style="Card.TLabel", width=6).pack(side="left")
            e = ttk.Entry(r, width=44)
            e.insert(0, rtsp_url(sub))
            e.configure(state="readonly")
            e.pack(side="left", fill="x", expand=True)
            ttk.Button(r, text="Copy", width=6,
                       command=lambda u=rtsp_url(sub): self.copy(u)).pack(side="left", padx=4)

        ttk.Button(wrap, text="Open SD browser instead",
                   command=lambda: self.nb.select(3)).pack(anchor="w", pady=10)

    def tab_settings(self, nb):
        wrap = ttk.Frame(nb, padding=8)
        nb.add(wrap, text="Settings")
        sc = self._scrollable(wrap)

        # ---- startup ----
        c1 = self._card(sc, "Startup", pad=13)
        c1.pack(fill="x", pady=(0, 8))
        self.set_login = tk.BooleanVar(value=os.path.exists(AUTOSTART_FILE))
        ttk.Checkbutton(c1, text="Start Imou Camera when I log in",
                        variable=self.set_login,
                        command=self.toggle_login).pack(anchor="w")
        self.set_preview = tk.BooleanVar(
            value=bool(self.settings.get("autostart_preview", True)))
        ttk.Checkbutton(c1, text="Open live preview automatically",
                        variable=self.set_preview,
                        command=self.save_prefs).pack(anchor="w", pady=(8, 0))

        # ---- local recording ----
        c2 = self._card(sc, "Local recording (this PC)", pad=13)
        c2.pack(fill="x", pady=(0, 8))
        self.set_rec = tk.BooleanVar(value=False)
        ttk.Checkbutton(c2, text="Record continuously",
                        variable=self.set_rec,
                        command=self.toggle_recorder).pack(anchor="w")

        env = read_env()
        r1 = ttk.Frame(c2, style="Card.TFrame")
        r1.pack(fill="x", pady=(10, 0))
        ttk.Label(r1, text="Segment", style="Card.TLabel", width=11).pack(side="left")
        self.set_segment = tk.StringVar(
            value=str(max(1, int(env.get("IMOU_SEGMENT", "300")) // 60)))
        ttk.Combobox(r1, textvariable=self.set_segment, width=5, state="readonly",
                     values=["1", "2", "5", "10", "15", "30"]).pack(side="left")
        ttk.Label(r1, text="min", style="Card.TLabel").pack(side="left", padx=4)

        r2 = ttk.Frame(c2, style="Card.TFrame")
        r2.pack(fill="x", pady=(6, 0))
        ttk.Label(r2, text="Keep for", style="Card.TLabel", width=11).pack(side="left")
        self.set_retain = tk.StringVar(value=env.get("IMOU_RETAIN_DAYS", "30"))
        ttk.Combobox(r2, textvariable=self.set_retain, width=5, state="readonly",
                     values=["1", "3", "7", "14", "30", "60", "90"]).pack(side="left")
        ttk.Label(r2, text="days", style="Card.TLabel").pack(side="left", padx=4)

        ttk.Button(c2, text="Apply", style="Accent.TButton",
                   command=self.apply_rec_config).pack(anchor="w", pady=(12, 0))

        # ---- camera SD recording ----
        c3 = self._card(sc, "Camera SD recording", pad=13)
        c3.pack(fill="x", pady=(0, 8))
        self.set_sdrec = tk.BooleanVar(value=False)
        ttk.Checkbutton(c3, text="Record to the camera's own SD card",
                        variable=self.set_sdrec,
                        command=self.toggle_sd_record).pack(anchor="w")
        self.sdrec_state = ttk.Label(c3, text="checking…", style="Muted.TLabel")
        self.sdrec_state.pack(anchor="w", pady=(6, 0))

        # ---- folders ----
        c4 = self._card(sc, "Folders", pad=13)
        c4.pack(fill="x", pady=(0, 8))
        for label, path in (("Recordings", OUTDIR), ("Snapshots", SNAPDIR)):
            r = ttk.Frame(c4, style="Card.TFrame")
            r.pack(fill="x", pady=2)
            ttk.Label(r, text=label, style="Card.TLabel", width=11).pack(side="left")
            ttk.Button(r, text="Open",
                       command=lambda p=path: self.open_dir(p)).pack(side="left")

        # ---- advanced ----
        c5 = self._card(sc, "Advanced", pad=13)
        c5.pack(fill="x")
        brow = ttk.Frame(c5, style="Card.TFrame")
        brow.pack(fill="x")
        for label, cmd in (("View log", self.view_log),
                           ("Reset preferences", self.reset_prefs)):
            ttk.Button(brow, text=label, command=cmd).pack(side="left", padx=(0, 6))

        # must run last: binds the wheel to every card/widget built above
        self._bind_wheel(sc)

    def _scrollable(self, parent):
        """Scrollable container. Returns the inner frame to build content into.

        The wheel must be bound to every CHILD widget too (see _bind_wheel):
        the cards sit on top of the canvas and swallow the events, so binding
        the canvas alone produces a scrollbar that drags but a wheel that does
        nothing.
        """
        canvas = tk.Canvas(parent, bg=BG, highlightthickness=0, bd=0,
                           yscrollincrement=24)
        vs = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vs.set)
        canvas.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        self._settings_canvas = canvas

        state = {"bar": True}

        def sync(_=None):
            box = canvas.bbox("all")
            canvas.configure(scrollregion=box)
            if not box:
                return
            # hide the scrollbar entirely when the content already fits --
            # a full-height thumb that moves 20px reads as "broken"
            need = (box[3] - box[1]) > canvas.winfo_height() + 1
            if need and not state["bar"]:
                vs.pack(side="right", fill="y")
                state["bar"] = True
            elif not need and state["bar"]:
                vs.pack_forget()
                state["bar"] = False

        inner.bind("<Configure>", sync)
        canvas.bind("<Configure>",
                    lambda e: (canvas.itemconfigure(win, width=e.width), sync()))

        def wheel(ev):
            try:
                num = int(getattr(ev, "num", 0) or 0)
            except (TypeError, ValueError):
                num = 0
            if num == 4:
                step = -3
            elif num == 5:
                step = 3
            else:
                step = -3 if getattr(ev, "delta", 0) > 0 else 3
            canvas.yview_scroll(step, "units")
            return "break"

        self._wheel_handler = wheel
        canvas.bind("<Button-4>", wheel)
        canvas.bind("<Button-5>", wheel)
        return inner

    def _bind_wheel(self, widget):
        """Recursively attach the wheel handler so scrolling works over cards."""
        for seq in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
            widget.bind(seq, self._wheel_handler)
        for child in widget.winfo_children():
            self._bind_wheel(child)

    # ---------- settings actions ----------
    def save_prefs(self, *_):
        if hasattr(self, "set_preview"):
            self.settings["autostart_preview"] = bool(self.set_preview.get())
        self.settings["preview_stream"] = self.stream_var.get()
        save_settings(self.settings)
        self.set_status("preferences saved", OK)

    def toggle_login(self):
        on = self.set_login.get()
        try:
            if on:
                os.makedirs(os.path.dirname(AUTOSTART_FILE), exist_ok=True)
                with open(AUTOSTART_FILE, "w", encoding="utf-8") as f:
                    f.write("[Desktop Entry]\nType=Application\n"
                            "Name=Imou Camera Control\n"
                            f"Exec={LAUNCHER}\n"
                            "Icon=imou-camera\nTerminal=false\n"
                            "X-GNOME-Autostart-enabled=true\n")
                self.set_status("will start at login", OK)
            else:
                if os.path.exists(AUTOSTART_FILE):
                    os.remove(AUTOSTART_FILE)
                self.set_status("login autostart removed", OK)
        except Exception as e:
            messagebox.showerror("Autostart", str(e))
            self.set_login.set(not on)

    def _svc_active(self):
        r = subprocess.run(["systemctl", "--user", "is-active", "imou-recorder.service"],
                           capture_output=True, text=True)
        return r.stdout.strip() == "active"

    def toggle_recorder(self):
        on = self.set_rec.get()
        verbs = ["enable", "--now"] if on else ["disable", "--now"]
        try:
            subprocess.run(["systemctl", "--user", *verbs, "imou-recorder.service"],
                           capture_output=True, timeout=25)
            self.set_status("recorder " + ("started" if on else "stopped"), OK)
        except Exception as e:
            self.set_status(f"recorder error: {e}", BAD)
        self.after(1400, self.sync_settings)
        self.after(1400, self.refresh_rec)

    def apply_rec_config(self):
        try:
            seg, days = int(self.set_segment.get()) * 60, int(self.set_retain.get())
        except ValueError:
            return
        try:
            write_env({"IMOU_SEGMENT": seg, "IMOU_RETAIN_DAYS": days})
            if self._svc_active():
                subprocess.run(["systemctl", "--user", "restart",
                                "imou-recorder.service"],
                               capture_output=True, timeout=25)
            self.set_status(f"recording: {seg // 60} min segments, {days} d retention", OK)
            self.after(1400, self.refresh_rec)
        except Exception as e:
            messagebox.showerror("Recording settings", str(e))

    def toggle_sd_record(self):
        want = bool(self.set_sdrec.get())

        def work():
            try:
                t = CAM.get_config("Record")
                if not t:
                    raise RuntimeError("Record config unavailable")
                tbl = json.loads(json.dumps(t))
                for row in tbl[0].get("TimeSection") or []:
                    for i in range(len(row)):
                        row[i] = ("393222 00:00:00-23:59:59"
                                  if (want and i == 0) else "0 00:00:00-23:59:59")
                ok = bool(CAM.set_config("Record", tbl).get("result"))
                self.set_status("SD recording " + ("on" if want else "off") if ok
                                else "SD recording: apply failed", OK if ok else BAD)
                self.after(0, lambda: self.set_sdrec.set(want if ok else not want))
                if ok:
                    self.after(400, self.sync_settings)
            except Exception as e:
                self.set_status(f"SD recording error: {e}", BAD)
                self.after(0, lambda: self.set_sdrec.set(not want))
        self.bg(work)

    def _read_sd_rec(self, tries=3):
        """True/False for camera SD recording, or None if every read failed.

        Retried because this runs alongside the other startup queries and one
        DVRIP login can lose the race for a session slot on this camera. Never
        return False for an unreadable config -- that would display a confident
        'disabled' for a camera that is actually recording.
        """
        for attempt in range(tries):
            try:
                t = CAM.get_config("Record")
                if t:
                    ts = t[0].get("TimeSection") or []
                    return any(int(s.split()[0]) != 0 for row in ts for s in row)
            except Exception:
                pass
            if attempt < tries - 1:
                time.sleep(1.5)
        return None

    def sync_settings(self):
        def work():
            active = self._svc_active()
            on = self._read_sd_rec()

            def show():
                self.set_rec.set(active)
                if on is not None:
                    self.set_sdrec.set(on)
                    self.sdrec_state.config(
                        text="currently " + ("recording" if on else "disabled"))
            self.after(0, show)
        self.bg(work)

    def open_dir(self, path):
        try:
            os.makedirs(path, exist_ok=True)
            subprocess.Popen(["xdg-open", path])
        except Exception as e:
            messagebox.showerror("Open folder", str(e))

    def view_log(self):
        try:
            if not os.path.exists(LOG_FILE):
                open(LOG_FILE, "a").close()
            subprocess.Popen(["xdg-open", LOG_FILE])
        except Exception as e:
            messagebox.showerror("Log", str(e))

    def reset_prefs(self):
        if not messagebox.askyesno("Reset preferences",
                                   "Reset app preferences to defaults?"):
            return
        self.settings = dict(DEFAULT_SETTINGS)
        save_settings(self.settings)
        self.set_preview.set(self.settings["autostart_preview"])
        self.set_status("preferences reset", OK)

    # ---------- helpers ----------
    def copy(self, text):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.lbl_status.config(text="copied")

    def bg(self, fn, *a):
        threading.Thread(target=fn, args=a, daemon=True).start()

    def set_status(self, txt, color=None):
        def do():
            self.lbl_status.config(text=txt)
            self.led.itemconfig(self.led_dot, fill=color or WARN)
        self.after(0, do)

    # ---------- video ----------
    def _autostart(self):
        if not self.settings.get("autostart_preview", True):
            self.set_status("preview off · press Play", MUTED)
            return
        if not (self.video and self.video.is_alive()):
            self.start_video()

    def toggle_video(self):
        if self.video and self.video.is_alive():
            self.stop_video()
        else:
            self.start_video()

    def start_video(self):
        sub = 0 if self.stream_var.get().startswith("Main") else 1
        self.canvas.delete("hint")
        self.video = VideoThread(rtsp_url(sub), self.frame_q)
        self.video.start()
        self.btn_play.config(text="Stop")
        self.set_status(f"live · {'main' if sub == 0 else 'sub'}", OK)

    def stop_video(self):
        if self.video:
            self.video.halt()
            self.video = None
        self.btn_play.config(text="Play")
        self.canvas.delete("all")
        self.preview.img_id = None
        self.canvas.create_text(PREVIEW_W // 2, PREVIEW_H // 2, text="stopped",
                                fill="#333a45", font=(FONT, 20), tags="hint")
        self.set_status("stopped", MUTED)

    def restart_video(self):
        if self.video and self.video.is_alive():
            self.stop_video()
            self.after(400, self.start_video)

    def _pump(self):
        if self.preview.img_id is not None or not self.canvas.find_withtag("hint"):
            self.preview.pump()
        self.after(30, self._pump)

    def open_hd(self):
        sub = 0 if self.stream_var.get().startswith("Main") else 1
        if not os.path.exists(FFPLAY):
            messagebox.showerror("Player missing",
                                 f"ffplay not found at:\n{FFPLAY}")
            return
        try:
            subprocess.Popen([FFPLAY, "-hide_banner", "-loglevel", "warning",
                              "-rtsp_transport", "tcp", "-window_title",
                              f"Imou {HOST}", rtsp_url(sub)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.set_status("HD player launched", OK)
        except Exception as e:
            messagebox.showerror("Player failed", str(e))

    def snapshot(self):
        def work():
            os.makedirs(SNAPDIR, exist_ok=True)
            fn = os.path.join(SNAPDIR, time.strftime("imou-%Y%m%d-%H%M%S.jpg"))
            r = subprocess.run(
                ["curl", "-s", "--digest", "-u", f"{USER}:{PASS}", "-m", "15",
                 "-o", fn, f"http://{HOST}/onvifsnapshot/media_service/snapshot"
                           f"?channel=1&subtype=0"], capture_output=True)
            if os.path.exists(fn) and os.path.getsize(fn) > 1000:
                self.set_status(f"snapshot saved", OK)
                subprocess.Popen(["xdg-open", fn])
            else:
                self.set_status("snapshot failed", BAD)
        self.bg(work)

    # ---------- state ----------
    def _load_state(self):
        self.bg(self._load_all)

    def _load_all(self):
        try:
            model = CAM.device_type()
            st = CAM.storage()
            self.after(0, lambda: self.lbl_dev.config(
                text=f"{model} · {HOST}" + (f" · SD {human(st['used'])}/{human(st['total'])}"
                                            if st else "")))
            self.set_status("online", OK)
        except Exception as e:
            self.set_status(f"offline: {e}", BAD)
        self.reload_image()
        self.reload_night()
        self.reload_motion()
        self.load_storage()
        self.refresh_rec()
        self.sync_settings()

    # ---- image ----
    def reload_image(self):
        def work():
            t = CAM.get_config("VideoColor")
            if not t:
                return
            self.vc_tbl = t
            e = t[0][0]
            def apply():
                for key, var in self.img_sliders:
                    var.set(int(e.get(key, 50)))
                self.style_var.set(e.get("Style", "Standard"))
            self.after(0, apply)
        self.bg(work)

    def apply_image(self):
        if not self.vc_tbl:
            return
        def work():
            tbl = json.loads(json.dumps(self.vc_tbl))
            for key, var in self.img_sliders:
                tbl[0][0][key] = int(var.get())
            tbl[0][0]["Style"] = self.style_var.get()
            r = CAM.set_config("VideoColor", tbl)
            self.set_status("image applied" if r.get("result") else "apply failed",
                            OK if r.get("result") else BAD)
        self.bg(work)

    # ---- night ----
    def reload_night(self):
        def work():
            t = CAM.get_config("VideoInOptions")
            if not t:
                return
            self.vio_tbl = t
            e = t[0]
            dnc = {0: "Color", 1: "Auto", 2: "Black & white"}.get(
                e.get("DayNightColor", 1), "Auto")
            def apply():
                self.dnc_var.set(dnc)
                self.dns_var.set(int(e.get("DayNightSensitivity", 2)))
                self.wb_var.set(e.get("WhiteBalance", "Auto"))
                self.af_var.set({0: "Off", 1: "50 Hz", 2: "60 Hz"}.get(
                    e.get("AntiFlicker", 0), "Off"))
                self.ir_var.set(bool(e.get("InfraRed", False)))
                self.mirror_var.set(bool(e.get("Mirror", False)))
                self.flip_var.set(bool(e.get("Flip", False)))
            self.after(0, apply)
        self.bg(work)

    def apply_night(self):
        if not self.vio_tbl:
            return
        def work():
            tbl = json.loads(json.dumps(self.vio_tbl))
            e = tbl[0]
            e["DayNightColor"] = {"Color": 0, "Auto": 1, "Black & white": 2}.get(
                self.dnc_var.get(), 1)
            e["DayNightSensitivity"] = int(self.dns_var.get())
            e["WhiteBalance"] = self.wb_var.get()
            e["AntiFlicker"] = {"Off": 0, "50 Hz": 1, "60 Hz": 2}.get(self.af_var.get(), 0)
            e["InfraRed"] = bool(self.ir_var.get())
            e["Mirror"] = bool(self.mirror_var.get())
            e["Flip"] = bool(self.flip_var.get())
            # keep the day/night sub-profiles in sync so the setting sticks
            for k in ("NormalOptions", "NightOptions"):
                sub = e.get(k)
                if isinstance(sub, dict):
                    for f in ("DayNightColor", "WhiteBalance", "Mirror", "Flip"):
                        if f in sub:
                            sub[f] = e[f]
            r = CAM.set_config("VideoInOptions", tbl)
            self.set_status("optics applied" if r.get("result") else "apply failed",
                            OK if r.get("result") else BAD)
        self.bg(work)

    # ---- motion ----
    def reload_motion(self):
        def work():
            t = CAM.get_config("MotionDetect")
            if not t:
                return
            self.md_tbl = t
            e = t[0]
            def apply():
                self.md_enable.set(bool(e.get("Enable", False)))
                self.md_level.set(int(e.get("Level", 3)))
                self.md_state.config(
                    text="currently " + ("ENABLED" if e.get("Enable") else "disabled"))
            self.after(0, apply)
        self.bg(work)

    def apply_motion(self):
        if not self.md_tbl:
            return
        def work():
            tbl = json.loads(json.dumps(self.md_tbl))
            tbl[0]["Enable"] = bool(self.md_enable.get())
            tbl[0]["Level"] = int(self.md_level.get())
            r = CAM.set_config("MotionDetect", tbl)
            ok = r.get("result")
            self.set_status("motion applied" if ok else "apply failed", OK if ok else BAD)
            if ok:
                self.after(0, lambda: self.md_state.config(
                    text="currently " + ("ENABLED" if self.md_enable.get() else "disabled")))
        self.bg(work)

    # ---- storage ----
    def load_storage(self):
        days = int(self.days_var.get())

        def work():
            try:
                st = CAM.storage()
                if st and st["total"]:
                    pct = st["used"] / st["total"] * 100
                    self.after(0, lambda: (
                        self.sd_bar.configure(value=pct),
                        self.sd_text.config(
                            text=f"{human(st['used'])} of {human(st['total'])} used "
                                 f"({pct:.0f}%) · {human(st['total'] - st['used'])} free "
                                 f"· {st['state']}")))
                files = CAM.list_recordings(days)
                total = sum(f.get("Length", 0) for f in files)

                def fill():
                    self.sd_tree.delete(*self.sd_tree.get_children())
                    for f in files:
                        flags = ",".join(f.get("Flags") or [])
                        ev = ",".join(f.get("Events") or [])
                        self.sd_tree.insert("", "end", values=(
                            f.get("StartTime", ""), f.get("EndTime", "")[11:],
                            f"{f.get('Duration', 0)}s", human(f.get("Length", 0)),
                            flags + (f"/{ev}" if ev else "")))
                    self.sd_summary.config(
                        text=f"{len(files)} clips in last {days} day(s), "
                             f"{human(total)} total")
                self.after(0, fill)
            except Exception as e:
                self.after(0, lambda: self.sd_summary.config(text=f"error: {e}"))
        self.bg(work)

    # ---- local recorder ----
    def svc(self, action):
        subprocess.run(["systemctl", "--user", action, "imou-recorder.service"],
                       capture_output=True)
        self.after(1200, self.refresh_rec)

    def refresh_rec(self):
        def work():
            r = subprocess.run(["systemctl", "--user", "is-active",
                                "imou-recorder.service"], capture_output=True, text=True)
            active = r.stdout.strip() == "active"
            files = []
            if os.path.isdir(OUTDIR):
                for n in sorted(os.listdir(OUTDIR))[-200:]:
                    if n.endswith(".mp4"):
                        p = os.path.join(OUTDIR, n)
                        files.append((n, os.path.getsize(p)))
            size = sum(s for _, s in files)

            def show():
                self.rec_state.config(
                    text=("● recording" if active else "○ stopped"),
                    foreground=OK if active else BAD)
                self.rec_info.config(
                    text=f"{OUTDIR}\n{len(files)} files · {human(size)} on disk")
                self.rec_tree.delete(*self.rec_tree.get_children())
                for n, s in reversed(files[-60:]):
                    self.rec_tree.insert("", "end", values=(n, human(s)))
            self.after(0, show)
        self.bg(work)

    def on_close(self):
        if self.video:
            self.video.halt()
        self.destroy()


def main():
    if not PASS:
        print("set IMOU_PASS", file=sys.stderr)
        return 2
    App().mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
