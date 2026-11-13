#!/usr/bin/env python3
"""Interfaz de la camara asicam (vista en vivo y ajustes tipo ASIStudio).

Se conecta al servidor asicam de la Raspberry, muestra el video crudo, el
histograma y permite cambiar formato, bin, resolucion, exposicion, ganancia y
los ajustes avanzados. Guarda fotos (FITS/PGM) y video (.ser).

Uso:
    python asicam/pc/asicam_gui.py                       # ultimo servidor usado (o lo busca)
    python asicam/pc/asicam_gui.py --host 192.168.200.159
    python asicam/pc/asicam_gui.py --simulate            # con un servidor simulado local
    python asicam/pc/asicam_gui.py --simulate --selftest # prueba automatica y sale

Requiere numpy (no usa Pillow: la imagen se pasa a Tk como PGM).
"""

import argparse
import json
import math
import os
import queue
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from asicam_client import AsiCam, AsiCamError, discover, save_fits, save_pgm  # noqa: E402

APP_NAME = "UFOMOUNT CAM"
CONFIG_PATH = Path.home() / ".asicam_gui.json"
DEFAULT_FOLDER = Path.home() / "Pictures" / "asicam"

# mismos colores que la interfaz del PTZ, para fusionarlas despues
C_BG = "#14191F"
C_PANEL = "#1B222A"
C_PANEL2 = "#232C34"
C_TEXT = "#E4E9ED"
C_MUTED = "#92A0AC"
C_ACC = "#64B5F6"
C_OK = "#7FC698"
C_ERR = "#E57373"
C_WARN = "#D9A93F"
C_LINE = "#2A343D"
C_REC = "#C2473F"

EXP_RANGES = [("32 µs – 1 ms", 32, 1000), ("1 – 100 ms", 1000, 100000),
              ("100 ms – 1 s", 100000, 1000000), ("1 – 10 s", 1000000, 10000000)]
RES_PRESETS = [(1600, 900), (1280, 720), (1024, 768), (800, 600), (640, 480), (320, 240)]
FLIPS = ["Ninguno", "Horizontal", "Vertical", "Ambos"]
REC_LIMITS = [("Sin límite", 0, 0), ("10 s", 0, 10), ("30 s", 0, 30), ("60 s", 0, 60),
              ("100 cuadros", 100, 0), ("1000 cuadros", 1000, 0)]
SEND_DELAY_MS = 200
TICK_MS = 30


def load_cfg():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_cfg(cfg):
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def fmt_exp(us):
    if us < 1000:
        return "%d µs" % us
    if us < 1000000:
        return "%g ms" % round(us / 1000.0, 3)
    return "%g s" % round(us / 1e6, 3)


# ----------------------------------------------------------------------------
# Video .ser (formato habitual de captura en astronomia)
# ----------------------------------------------------------------------------

def _ticks(unix_s):
    """Fecha en 'ticks' de .NET (100 ns desde el 1/1/0001), como pide el formato SER."""
    return int(unix_s * 10 ** 7) + 621355968000000000


class SerWriter:
    HDR = struct.Struct("<14siiiiiii40s40s40sqq")

    def __init__(self, path, width, height, depth, instrument="", observer=""):
        self.path = path
        self.width, self.height, self.depth = width, height, depth
        self.count = 0
        self.stamps = []
        self.f = open(path, "wb")
        now = time.time()
        local = now - time.timezone if not time.localtime(now).tm_isdst else now - time.altzone
        self._fields = [b"LUCAM-RECORDER", 0, 0, 1, width, height, depth, 0,
                        observer.encode("ascii", "replace")[:40].ljust(40, b"\0"),
                        instrument.encode("ascii", "replace")[:40].ljust(40, b"\0"),
                        b"".ljust(40, b"\0"), _ticks(local), _ticks(now)]
        self.f.write(self.HDR.pack(*self._fields))

    def add(self, frame):
        self.f.write(frame.image.astype("<u2" if self.depth == 16 else np.uint8, copy=False).tobytes())
        self.stamps.append(_ticks(frame.timestamp))
        self.count += 1

    def close(self):
        if self.f is None:
            return
        self.f.write(struct.pack("<%dq" % len(self.stamps), *self.stamps))
        self._fields[7] = self.count
        self.f.seek(0)
        self.f.write(self.HDR.pack(*self._fields))
        self.f.close()
        self.f = None


def read_ser_header(path):
    with open(path, "rb") as f:
        h = SerWriter.HDR.unpack(f.read(SerWriter.HDR.size))
        size = os.path.getsize(path)
    return {"id": h[0], "width": h[4], "height": h[5], "depth": h[6], "frames": h[7], "size": size}


class Recorder:
    """Graba en .ser todos los cuadros que llegan (en su propio hilo)."""

    def __init__(self):
        self.q = queue.Queue(maxsize=64)
        self.active = False
        self.writer = None
        self.thread = None
        self.frames = self.lost = 0
        self.t0 = 0.0
        self.path = None
        self.error = None
        self.done = False
        self.limit_frames = self.limit_secs = 0
        self.instrument = ""

    def start(self, path, limit_frames=0, limit_secs=0, instrument=""):
        self.path = path
        self.limit_frames, self.limit_secs = limit_frames, limit_secs
        self.instrument = instrument
        self.frames = self.lost = 0
        self.error = None
        self.done = False
        self.writer = None
        while not self.q.empty():
            self.q.get_nowait()
        self.t0 = time.monotonic()
        self.active = True
        self.thread = threading.Thread(target=self._run, name="grabacion", daemon=True)
        self.thread.start()

    def offer(self, frame):  # llamado desde el hilo receptor: debe ser rapido
        if not self.active:
            return
        try:
            self.q.put_nowait(frame)
        except queue.Full:
            self.lost += 1

    def stop(self):
        if self.active:
            self.active = False
            self.q.put(None)

    def elapsed(self):
        return time.monotonic() - self.t0

    def _run(self):
        try:
            while True:
                f = self.q.get()
                if f is None:
                    break
                if self.writer is None:
                    depth = 16 if f.image.dtype != np.uint8 else 8
                    self.writer = SerWriter(self.path, f.width, f.height, depth, self.instrument, "asicam")
                if (f.width, f.height) != (self.writer.width, self.writer.height):
                    self.error = "cambió el tamaño de imagen: grabación cortada"
                    break
                self.writer.add(f)
                self.frames += 1
                if (self.limit_frames and self.frames >= self.limit_frames) or \
                        (self.limit_secs and self.elapsed() >= self.limit_secs):
                    break
        except OSError as e:
            self.error = "no pude escribir: %s" % e
        finally:
            self.active = False
            if self.writer is not None:
                try:
                    self.writer.close()
                except OSError as e:
                    self.error = self.error or "no pude cerrar el archivo: %s" % e
            self.done = True


# ----------------------------------------------------------------------------
# Hilo para las llamadas de control (bloqueantes) sin trabar la interfaz
# ----------------------------------------------------------------------------

class Worker(threading.Thread):
    def __init__(self):
        super().__init__(name="control", daemon=True)
        self.q = queue.Queue()
        self.results = queue.Queue()
        self.start()

    def submit(self, fn, ok=None, err=None):
        self.q.put((fn, ok, err))

    def run(self):
        while True:
            fn, ok, err = self.q.get()
            try:
                r = fn()
                self.results.put((ok, r))
            except Exception as e:  # noqa: BLE001 - se informa en la interfaz
                self.results.put((err, e))

    def pump(self):
        """Llama (en el hilo de Tk) los callbacks de las tareas terminadas."""
        while True:
            try:
                cb, val = self.results.get_nowait()
            except queue.Empty:
                return
            if cb is not None:
                cb(val)


# ----------------------------------------------------------------------------
# Interfaz
# ----------------------------------------------------------------------------

class AsiCamGui:
    def __init__(self, root, host=None, selftest=False, hold=0.0):
        self.root = root
        self.cfg = load_cfg()
        self.worker = Worker()
        self.recorder = Recorder()
        self.cam = None
        self.info = None
        self.controls = {}
        self.settings = {}
        self.connecting = False
        self._updating = False          # True mientras se cargan valores en los widgets
        self._pending = {}              # cambios por mandar
        self._send_job = None
        self._status_busy = False
        self._status_t = 0.0
        self._server_status = {}
        self._shown = None              # ultimo cuadro dibujado
        self._photo = None
        self._img_item = None
        self._view = None
        self._disp_times = []
        self._hist_n = 0
        self._pan = [0, 0]
        self._drag = None
        self._res_map = {}
        self.selftest = selftest
        self.hold = hold
        self.test = None
        self.exit_code = 0
        self._label_t = 0.0
        self._rate_t = 0.0
        self._rate_b = 0
        self._mbps = 0.0
        self._cam_det_base = ""
        self._build_ui(host)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._tick()
        if host or self.cfg.get("host"):
            self.root.after(200, self.connect)

    # ------------------------------------------------------------ construccion
    def _build_ui(self, host):
        r = self.root
        r.title(APP_NAME)
        r.configure(bg=C_BG)
        r.geometry("1360x860")
        r.minsize(1000, 680)
        st = ttk.Style(r)
        st.theme_use("clam")
        st.configure("TFrame", background=C_BG)
        st.configure("Panel.TFrame", background=C_PANEL)
        st.configure("TLabel", background=C_BG, foreground=C_TEXT)
        st.configure("Panel.TLabel", background=C_PANEL, foreground=C_TEXT)
        st.configure("Muted.TLabel", background=C_PANEL, foreground=C_MUTED)
        st.configure("Head.TLabel", background=C_PANEL, foreground=C_MUTED, font=("Segoe UI", 8, "bold"))
        st.configure("TButton", background=C_PANEL2, foreground=C_TEXT, borderwidth=0,
                     focuscolor=C_PANEL2, padding=5)
        st.map("TButton", background=[("active", C_LINE), ("disabled", C_PANEL)],
               foreground=[("disabled", C_MUTED)])
        st.configure("TCheckbutton", background=C_PANEL, foreground=C_TEXT)
        st.map("TCheckbutton", background=[("active", C_PANEL)])
        st.configure("TCombobox", fieldbackground=C_PANEL2, background=C_PANEL2, foreground=C_TEXT,
                     arrowcolor=C_TEXT, bordercolor=C_LINE, lightcolor=C_PANEL2, darkcolor=C_PANEL2,
                     selectbackground=C_PANEL2, selectforeground=C_TEXT)
        st.map("TCombobox", fieldbackground=[("readonly", C_PANEL2), ("disabled", C_PANEL)],
               foreground=[("disabled", C_MUTED)], background=[("active", C_LINE)],
               selectbackground=[("readonly", C_PANEL2)], selectforeground=[("readonly", C_TEXT)])
        st.configure("Horizontal.TScale", background="#5A6B7A", troughcolor=C_PANEL2, bordercolor=C_LINE,
                     lightcolor="#5A6B7A", darkcolor="#5A6B7A")
        st.map("Horizontal.TScale", background=[("active", C_ACC), ("disabled", C_PANEL)])
        st.configure("Vertical.TScrollbar", background=C_PANEL2, troughcolor=C_PANEL, arrowcolor=C_MUTED)
        r.option_add("*TCombobox*Listbox.background", C_PANEL2)
        r.option_add("*TCombobox*Listbox.foreground", C_TEXT)
        r.option_add("*TCombobox*Listbox.selectBackground", C_LINE)

        r.columnconfigure(0, weight=1)
        r.rowconfigure(1, weight=1)

        # ---- barra superior ----
        top = ttk.Frame(r, style="Panel.TFrame", padding=8)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=(8, 4))
        top.columnconfigure(5, weight=1)
        ttk.Label(top, text=APP_NAME, style="Panel.TLabel", font=("Segoe UI", 13, "bold")).grid(
            row=0, column=0, padx=(2, 14))
        ttk.Label(top, text="Servidor:", style="Muted.TLabel").grid(row=0, column=1)
        self.host_var = tk.StringVar(value=host or self.cfg.get("host", ""))
        self.host_cb = ttk.Combobox(top, textvariable=self.host_var, width=22,
                                    values=self.cfg.get("hosts", []))
        self.host_cb.grid(row=0, column=2, padx=4)
        self.find_btn = ttk.Button(top, text="Buscar", command=self.find_servers)
        self.find_btn.grid(row=0, column=3, padx=(4, 0))
        self.conn_btn = ttk.Button(top, text="Conectar", command=self.toggle_conn)
        self.conn_btn.grid(row=0, column=4, padx=8)
        self.conn_lbl = tk.Label(top, text="desconectado", bg=C_PANEL2, fg=C_MUTED, padx=10, pady=2)
        self.conn_lbl.grid(row=0, column=6, sticky="e", padx=2)

        # ---- vista ----
        view = tk.Frame(r, bg="#000000", highlightthickness=0)
        view.grid(row=1, column=0, sticky="nsew", padx=(8, 4), pady=4)
        self.canvas = tk.Canvas(view, bg="#05070A", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._redraw())
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", lambda e: self.pix_lbl.configure(text=""))
        self.canvas.bind("<ButtonPress-1>", self._drag_start)
        self.canvas.bind("<B1-Motion>", self._drag_move)
        self.canvas.bind("<Double-Button-1>", lambda e: self._toggle_zoom())
        self.overlay = self.canvas.create_text(10, 10, anchor="nw", fill=C_TEXT, font=("Consolas", 10),
                                               text="Sin conexión")
        self.cross = [self.canvas.create_line(0, 0, 0, 0, fill="#FF5050", state="hidden"),
                      self.canvas.create_line(0, 0, 0, 0, fill="#FF5050", state="hidden")]

        # ---- panel derecho con scroll ----
        side = tk.Frame(r, bg=C_PANEL)
        side.grid(row=1, column=1, sticky="ns", padx=(4, 8), pady=4)
        sc = tk.Canvas(side, bg=C_PANEL, highlightthickness=0, width=340)
        sb = ttk.Scrollbar(side, orient="vertical", command=sc.yview)
        sc.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        sc.pack(side="left", fill="y", expand=True)
        p = tk.Frame(sc, bg=C_PANEL, padx=10, pady=8)
        sc.create_window(0, 0, window=p, anchor="nw", width=340)
        p.bind("<Configure>", lambda e: sc.configure(scrollregion=sc.bbox("all")))
        for w in (sc, p):
            w.bind("<Enter>", lambda e: sc.bind_all("<MouseWheel>", lambda ev: sc.yview_scroll(
                -1 if ev.delta > 0 else 1, "units")))
            w.bind("<Leave>", lambda e: sc.unbind_all("<MouseWheel>"))
        self.side = p

        def head(text, pady=(12, 4)):
            ttk.Label(p, text=text, style="Head.TLabel").pack(anchor="w", pady=pady)

        def row():
            f = tk.Frame(p, bg=C_PANEL)
            f.pack(fill="x", pady=2)
            return f

        def lbl(parent, text, width=12):
            return tk.Label(parent, text=text, bg=C_PANEL, fg=C_MUTED, width=width, anchor="w")

        def entry(parent, var, width=8):
            return tk.Entry(parent, textvariable=var, width=width, bg=C_PANEL2, fg=C_TEXT, bd=0,
                            insertbackground=C_TEXT, disabledbackground=C_PANEL,
                            disabledforeground=C_MUTED, justify="right")

        # CAMARA
        head("CÁMARA", pady=(0, 4))
        self.cam_name = ttk.Label(p, text="—", style="Panel.TLabel", font=("Segoe UI", 11, "bold"))
        self.cam_name.pack(anchor="w")
        self.cam_det = ttk.Label(p, text="", style="Muted.TLabel")
        self.cam_det.pack(anchor="w")

        # IMAGEN
        head("IMAGEN")
        f = row()
        lbl(f, "Formato", 8).pack(side="left")
        self.fmt_var = tk.StringVar()
        self.fmt_cb = ttk.Combobox(f, textvariable=self.fmt_var, width=7, state="readonly")
        self.fmt_cb.pack(side="left", padx=(0, 10))
        self.fmt_cb.bind("<<ComboboxSelected>>", lambda e: self._geometry_changed())
        lbl(f, "Bin", 4).pack(side="left")
        self.bin_var = tk.StringVar()
        self.bin_cb = ttk.Combobox(f, textvariable=self.bin_var, width=5, state="readonly")
        self.bin_cb.pack(side="left")
        self.bin_cb.bind("<<ComboboxSelected>>", lambda e: (self._fill_resolutions(), self._geometry_changed()))
        f = row()
        lbl(f, "Resolución", 10).pack(side="left")
        self.res_var = tk.StringVar()
        self.res_cb = ttk.Combobox(f, textvariable=self.res_var, width=18, state="readonly")
        self.res_cb.pack(side="left")
        self.res_cb.bind("<<ComboboxSelected>>", lambda e: self._geometry_changed())
        self.geom_widgets = [self.fmt_cb, self.bin_cb, self.res_cb]

        # HISTOGRAMA
        head("HISTOGRAMA")
        self.hist = tk.Canvas(p, width=318, height=96, bg=C_PANEL2, highlightthickness=0)
        self.hist.pack(anchor="w")
        f = row()
        self.hist_lbl = tk.Label(f, text="", bg=C_PANEL, fg=C_TEXT, font=("Consolas", 9), anchor="w")
        self.hist_lbl.pack(side="left")
        self.hist_log = tk.BooleanVar(value=self.cfg.get("hist_log", True))
        ttk.Checkbutton(f, text="log", variable=self.hist_log).pack(side="right")

        # CONTROL
        head("CONTROL")
        f = row()
        lbl(f, "Exposición", 10).pack(side="left")
        self.exp_var = tk.StringVar()
        self.exp_entry = entry(f, self.exp_var, 9)
        self.exp_entry.pack(side="left")
        tk.Label(f, text="ms", bg=C_PANEL, fg=C_MUTED).pack(side="left", padx=(2, 8))
        self.exp_auto = tk.BooleanVar()
        ttk.Checkbutton(f, text="Auto", variable=self.exp_auto, command=self._exp_auto_changed).pack(side="right")
        self.exp_entry.bind("<Return>", lambda e: self._exp_typed())
        self.exp_entry.bind("<FocusOut>", lambda e: self._exp_typed())
        f = row()
        self.exp_range = tk.StringVar(value=EXP_RANGES[self.cfg.get("exp_range", 1)][0])
        self.exp_range_cb = ttk.Combobox(f, textvariable=self.exp_range, width=14, state="readonly",
                                         values=[x[0] for x in EXP_RANGES])
        self.exp_range_cb.pack(side="left")
        self.exp_range_cb.bind("<<ComboboxSelected>>", lambda e: self._exp_range_changed())
        self.exp_scale = ttk.Scale(f, from_=0, to=1000, orient="horizontal", command=self._exp_slid)
        self.exp_scale.pack(side="left", fill="x", expand=True, padx=(8, 0))

        f = row()
        lbl(f, "Ganancia", 10).pack(side="left")
        self.gain_var = tk.StringVar()
        self.gain_entry = entry(f, self.gain_var, 9)
        self.gain_entry.pack(side="left")
        self.gain_auto = tk.BooleanVar()
        ttk.Checkbutton(f, text="Auto", variable=self.gain_auto, command=self._gain_auto_changed).pack(side="right")
        self.gain_entry.bind("<Return>", lambda e: self._gain_typed())
        self.gain_entry.bind("<FocusOut>", lambda e: self._gain_typed())
        f = row()
        self.gain_scale = ttk.Scale(f, from_=0, to=600, orient="horizontal", command=self._gain_slid)
        self.gain_scale.pack(fill="x", expand=True)

        self.adv_btn = ttk.Button(p, text="Avanzado ▸", command=self._toggle_adv)
        self.adv_btn.pack(anchor="w", pady=(8, 2))
        self.adv = tk.Frame(p, bg=C_PANEL)
        self.adv_vars = {}

        def adv_row(text, key, width=8, auto_key=None, note=None):
            fr = tk.Frame(self.adv, bg=C_PANEL)
            fr.pack(fill="x", pady=2)
            lbl(fr, text, 16).pack(side="left")
            v = tk.StringVar()
            e = entry(fr, v, width)
            e.pack(side="left")
            e.bind("<Return>", lambda ev, k=key: self._adv_typed(k))
            e.bind("<FocusOut>", lambda ev, k=key: self._adv_typed(k))
            self.adv_vars[key] = (v, e)
            if auto_key:
                av = tk.BooleanVar()
                ttk.Checkbutton(fr, text="Auto", variable=av,
                                command=lambda k=auto_key, b=av: self.queue_set(**{k: b.get()})).pack(side="right")
                self.adv_vars[auto_key] = (av, None)
            if note:
                tk.Label(self.adv, text=note, bg=C_PANEL, fg=C_MUTED, font=("Segoe UI", 8),
                         wraplength=310, justify="left").pack(anchor="w")

        adv_row("Offset", "offset")
        adv_row("Ancho de banda USB %", "bandwidth", auto_key="auto_bandwidth",
                note="60 % es estable en la Pi; 70 % con el servidor en tiempo real.")
        fr = tk.Frame(self.adv, bg=C_PANEL)
        fr.pack(fill="x", pady=2)
        self.hs_var = tk.BooleanVar()
        ttk.Checkbutton(fr, text="High speed (en la ASI220 Mini limita a ~10 fps)", variable=self.hs_var,
                        command=lambda: self.queue_set(high_speed=1 if self.hs_var.get() else 0)).pack(anchor="w")
        fr = tk.Frame(self.adv, bg=C_PANEL)
        fr.pack(fill="x", pady=2)
        lbl(fr, "Voltear", 16).pack(side="left")
        self.flip_var = tk.StringVar(value=FLIPS[0])
        cb = ttk.Combobox(fr, textvariable=self.flip_var, values=FLIPS, width=11, state="readonly")
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda e: self.queue_set(flip=FLIPS.index(self.flip_var.get())))
        adv_row("Auto: ganancia máx.", "auto_max_gain")
        adv_row("Auto: exposición máx. ms", "auto_max_exp_ms")
        adv_row("Auto: brillo objetivo", "auto_target")

        # VISTA
        head("VISTA")
        f = row()
        lbl(f, "Estirar", 8).pack(side="left")
        self.stretch = tk.StringVar(value=self.cfg.get("stretch", "Automático"))
        ttk.Combobox(f, textvariable=self.stretch, values=["Lineal", "Automático"], width=11,
                     state="readonly").pack(side="left", padx=(0, 10))
        lbl(f, "Zoom", 5).pack(side="left")
        self.zoom = tk.StringVar(value=self.cfg.get("zoom", "Ajustar"))
        zc = ttk.Combobox(f, textvariable=self.zoom, values=["Ajustar", "1:1"], width=7, state="readonly")
        zc.pack(side="left")
        zc.bind("<<ComboboxSelected>>", lambda e: self._redraw())
        f = row()
        self.cross_var = tk.BooleanVar(value=self.cfg.get("cross", False))
        ttk.Checkbutton(f, text="Cruz central", variable=self.cross_var, command=self._redraw).pack(side="left")
        tk.Label(f, text="doble clic: zoom · arrastrar: mover", bg=C_PANEL, fg=C_MUTED,
                 font=("Segoe UI", 8)).pack(side="right")

        # CAPTURA
        head("CAPTURA")
        f = row()
        lbl(f, "Carpeta", 8).pack(side="left")
        self.folder = tk.StringVar(value=self.cfg.get("folder", str(DEFAULT_FOLDER)))
        tk.Label(f, textvariable=self.folder, bg=C_PANEL, fg=C_TEXT, anchor="w", width=24).pack(side="left")
        ttk.Button(f, text="…", width=3, command=self._pick_folder).pack(side="right")
        f = row()
        self.photo_fmt = tk.StringVar(value=self.cfg.get("photo_fmt", "FITS"))
        ttk.Combobox(f, textvariable=self.photo_fmt, values=["FITS", "PGM"], width=6,
                     state="readonly").pack(side="left")
        self.snap_btn = ttk.Button(f, text="Foto", command=self.snapshot)
        self.snap_btn.pack(side="left", padx=6, fill="x", expand=True)
        f = row()
        self.rec_limit = tk.StringVar(value=self.cfg.get("rec_limit", REC_LIMITS[0][0]))
        ttk.Combobox(f, textvariable=self.rec_limit, values=[x[0] for x in REC_LIMITS], width=12,
                     state="readonly").pack(side="left")
        self.rec_btn = tk.Button(f, text="● Grabar .ser", bg=C_PANEL2, fg=C_TEXT, bd=0, padx=8, pady=4,
                                 activebackground=C_LINE, command=self.toggle_record)
        self.rec_btn.pack(side="left", padx=6, fill="x", expand=True)
        self.rec_lbl = tk.Label(p, text="", bg=C_PANEL, fg=C_MUTED, anchor="w", justify="left")
        self.rec_lbl.pack(fill="x")
        self.msg_lbl = tk.Label(p, text="", bg=C_PANEL, fg=C_MUTED, anchor="w", justify="left",
                                wraplength=310)
        self.msg_lbl.pack(fill="x", pady=(8, 0))

        # ---- barra de estado ----
        bar = tk.Frame(r, bg=C_PANEL)
        bar.grid(row=2, column=0, columnspan=2, sticky="ew", padx=8, pady=(0, 8))
        self.status_lbl = tk.Label(bar, text="", bg=C_PANEL, fg=C_MUTED, anchor="w", font=("Consolas", 9))
        self.status_lbl.pack(side="left", padx=6, pady=3)
        self.pix_lbl = tk.Label(bar, text="", bg=C_PANEL, fg=C_TEXT, anchor="e", font=("Consolas", 9))
        self.pix_lbl.pack(side="right", padx=6)
        self._set_controls_state(False)

    # ------------------------------------------------------------ utilidades UI
    def message(self, text, color=C_MUTED):
        self.msg_lbl.configure(text=text, fg=color)

    def _set_controls_state(self, on):
        st = "normal" if on else "disabled"
        for w in (self.exp_entry, self.gain_entry, self.snap_btn, self.rec_btn):
            w.configure(state=st)
        for w in self.geom_widgets + [self.exp_range_cb]:
            w.configure(state="readonly" if on else "disabled")
        for v, e in self.adv_vars.values():
            if e is not None:
                e.configure(state=st)
        for sc in (self.exp_scale, self.gain_scale):
            sc.state(["!disabled"] if on else ["disabled"])
        if on:
            self._apply_auto_states()

    def _toggle_adv(self):
        if self.adv.winfo_ismapped():
            self.adv.pack_forget()
            self.adv_btn.configure(text="Avanzado ▸")
        else:
            self.adv.pack(after=self.adv_btn, fill="x")
            self.adv_btn.configure(text="Avanzado ▾")

    def _pick_folder(self):
        d = filedialog.askdirectory(initialdir=self.folder.get() or str(Path.home()))
        if d:
            self.folder.set(d)
            self._save_prefs()

    def _save_prefs(self):
        if self.selftest:  # la prueba automatica no toca las preferencias del usuario
            return
        self.cfg.update({"host": self.host_var.get().strip(), "folder": self.folder.get(),
                         "stretch": self.stretch.get(), "zoom": self.zoom.get(), "cross": self.cross_var.get(),
                         "hist_log": self.hist_log.get(), "photo_fmt": self.photo_fmt.get(),
                         "rec_limit": self.rec_limit.get(),
                         "exp_range": [x[0] for x in EXP_RANGES].index(self.exp_range.get())
                         if self.exp_range.get() in [x[0] for x in EXP_RANGES] else 1})
        save_cfg(self.cfg)

    # ------------------------------------------------------------ conexion
    def find_servers(self):
        self.find_btn.configure(state="disabled")
        self.message("Buscando servidores asicam en la red…")
        hint = self.host_var.get().strip()
        self.worker.submit(lambda: discover(targets=[hint] if hint else []), self._found, self._found)

    def _found(self, res):
        self.find_btn.configure(state="normal")
        if isinstance(res, Exception) or not res:
            self.message("No encontré servidores asicam. ¿Está corriendo en la Pi?", C_WARN)
            return
        ips = [s["ip"] for s in res]
        self.host_cb.configure(values=ips)
        self.host_var.set(ips[0])
        self.message("Encontrado: " + ", ".join("%s (%s, %s)" % (s["ip"], s.get("host"), s.get("camera"))
                                                for s in res), C_OK)

    def toggle_conn(self):
        if self.cam is not None:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        if self.cam is not None or self.connecting:
            return
        host = self.host_var.get().strip()
        if not host:
            self.find_servers()
            return
        self.connecting = True
        self.conn_btn.configure(state="disabled")
        self.conn_lbl.configure(text="conectando…", fg=C_WARN)

        def do():
            cam = AsiCam(host, queue_size=4)
            try:
                return cam, cam.info(), cam.settings()
            except Exception:
                cam.close()
                raise
        self.worker.submit(do, self._connected, self._connect_failed)

    def _connect_failed(self, e):
        self.connecting = False
        self.conn_btn.configure(state="normal", text="Conectar")
        self.conn_lbl.configure(text="sin conexión", fg=C_ERR)
        self.message(str(e), C_ERR)
        if self.test:
            self.test.fail_now("no conecta: %s" % e)

    def _connected(self, res):
        cam, info, settings = res
        self.cam, self.info = cam, info
        self.controls = info.get("controls", {})
        self.connecting = False
        c = info["camera"]
        self.cam_name.configure(text=c["name"])
        self._cam_det_base = "%d×%d · %d bits · %.2f µm · %s" % (
            c["max_width"], c["max_height"], c["bit_depth"], c["pixel_um"], "color" if c["color"] else "mono")
        self.cam_det.configure(text=self._cam_det_base)
        self._rate_t, self._rate_b = 0.0, 0
        self.fmt_cb.configure(values=c["formats"])
        self.bin_cb.configure(values=["Bin%d" % b for b in c["bins"]])
        g = self.controls.get("gain")
        if g:
            self.gain_scale.configure(from_=g["min"], to=g["max"])
        self._apply_settings(settings)
        self._set_controls_state(True)
        cam.on_frame(self.recorder.offer)
        cam.start()
        host = self.host_var.get().strip()
        hosts = [host] + [h for h in self.cfg.get("hosts", []) if h != host]
        self.cfg["hosts"] = hosts[:8]
        self.host_cb.configure(values=self.cfg["hosts"])
        self._save_prefs()
        self.conn_btn.configure(state="normal", text="Desconectar")
        self.conn_lbl.configure(text="conectado a %s" % cam.server.get("host", host), fg=C_OK)
        self.message("Servidor asicam %s · SDK %s" % (cam.server.get("version"), cam.server.get("sdk")))
        if self.selftest and self.test is None:
            self.test = SelfTest(self)

    def disconnect(self):
        if self.recorder.active:
            self.recorder.stop()
        cam, self.cam = self.cam, None
        self._shown = None
        self._set_controls_state(False)
        self.conn_btn.configure(text="Conectar")
        self.conn_lbl.configure(text="desconectado", fg=C_MUTED)
        self.canvas.itemconfigure(self.overlay, text="Sin conexión")
        if cam is not None:
            self.worker.submit(cam.close)

    def on_close(self):
        self._save_prefs()
        self.recorder.stop()
        if self.recorder.thread is not None:
            self.recorder.thread.join(5)
        if self.cam is not None:
            try:
                self.cam.close()
            except Exception:  # noqa: BLE001
                pass
        self.root.destroy()

    # ------------------------------------------------------------ ajustes
    def queue_set(self, **changes):
        """Junta cambios y los manda al servidor tras una pausa corta."""
        if self._updating or self.cam is None:
            return
        self._pending.update(changes)
        if self._send_job is not None:
            self.root.after_cancel(self._send_job)
        self._send_job = self.root.after(SEND_DELAY_MS, self._send_pending)

    def _send_pending(self):
        self._send_job = None
        if not self._pending or self.cam is None:
            return
        changes, self._pending = self._pending, {}
        cam = self.cam
        self.worker.submit(lambda: cam.set(**changes), self._applied, self._set_failed)

    def _applied(self, settings):
        self._apply_settings(settings)

    def _set_failed(self, e):
        self.message("El servidor rechazó el cambio: %s" % e, C_ERR)
        if self.cam is not None:
            cam = self.cam
            self.worker.submit(cam.settings, self._applied)

    def _apply_settings(self, s):
        """Carga los ajustes del servidor en los widgets (sin volver a mandarlos)."""
        self.settings = dict(s)
        self._updating = True
        try:
            self.fmt_var.set(s.get("image_type", "RAW8"))
            self.bin_var.set("Bin%d" % s.get("bin", 1))
            self._fill_resolutions()
            key = (s.get("width", 0), s.get("height", 0))
            label = next((k for k, v in self._res_map.items() if v == key), None)
            if label is None and key != (0, 0):
                label = "%d×%d" % key
                self._res_map[label] = key
            self.res_var.set(label or next(iter(self._res_map)))
            self._show_exposure(s.get("exposure_us", 10000))
            self.exp_auto.set(bool(s.get("auto_exposure")))
            self.gain_var.set(str(s.get("gain") if s.get("gain") is not None else ""))
            if s.get("gain") is not None:
                self.gain_scale.set(s["gain"])
            self.gain_auto.set(bool(s.get("auto_gain")))
            for k, (v, e) in self.adv_vars.items():
                val = s.get(k)
                if e is None:
                    v.set(bool(val))
                else:
                    if val is None and k in self.controls:
                        val = self.controls[k]["default"]
                    v.set("" if val is None else str(val))
            self.hs_var.set(bool(s.get("high_speed")))
            self.flip_var.set(FLIPS[s.get("flip") or 0])
        finally:
            self._updating = False
        self._apply_auto_states()

    def _apply_auto_states(self):
        if self.cam is None:
            return
        self.exp_entry.configure(state="disabled" if self.exp_auto.get() else "normal")
        self.gain_entry.configure(state="disabled" if self.gain_auto.get() else "normal")
        for sc, auto in ((self.exp_scale, self.exp_auto), (self.gain_scale, self.gain_auto)):
            sc.state(["disabled"] if auto.get() else ["!disabled"])

    def _fill_resolutions(self):
        if not self.info:
            return
        c = self.info["camera"]
        try:
            b = int(self.bin_var.get().replace("Bin", "") or 1)
        except ValueError:
            b = 1
        mw, mh = c["max_width"] // b, c["max_height"] // b
        m = {"Máxima (%d×%d)" % (mw, mh): (0, 0)}
        for w, h in RES_PRESETS:
            if w < mw and h <= mh:
                m["%d×%d" % (w, h)] = (w, h)
        self._res_map = m
        self.res_cb.configure(values=list(m))
        if self.res_var.get() not in m:
            self.res_var.set(next(iter(m)))

    def _geometry_changed(self):
        if self._updating:
            return
        w, h = self._res_map.get(self.res_var.get(), (0, 0))
        self.queue_set(image_type=self.fmt_var.get(), bin=int(self.bin_var.get().replace("Bin", "")),
                       width=w, height=h, start_x=-1, start_y=-1)

    # exposicion (el campo esta en ms; el servidor usa microsegundos)
    def _exp_limits(self):
        c = self.controls.get("exposure_us", {"min": 32, "max": 10000000})
        return c["min"], c["max"]

    def _range_for(self, us):
        for name, lo, hi in EXP_RANGES:
            if us <= hi:
                return name, lo, hi
        return EXP_RANGES[-1]

    def _show_exposure(self, us, move_range=True):
        name, lo, hi = self._range_for(us)
        cur = next((x for x in EXP_RANGES if x[0] == self.exp_range.get()), None)
        if move_range and (cur is None or not (cur[1] <= us <= cur[2])):
            self.exp_range.set(name)
            cur = (name, lo, hi)
        _, lo, hi = cur
        self.exp_var.set(("%.3f" % (us / 1000.0)).rstrip("0").rstrip("."))
        pos = 1000.0 * math.log(max(us, lo) / lo) / math.log(hi / lo)
        was = self._updating
        self._updating = True
        self.exp_scale.set(max(0.0, min(1000.0, pos)))
        self._updating = was

    def _exp_typed(self):
        if self._updating or self.cam is None or self.exp_auto.get():
            return
        try:
            us = int(round(float(self.exp_var.get().replace(",", ".")) * 1000))
        except ValueError:
            self._show_exposure(self.settings.get("exposure_us", 10000))
            return
        lo, hi = self._exp_limits()
        us = max(lo, min(hi, us))
        self._show_exposure(us)
        if us != self.settings.get("exposure_us"):
            self.queue_set(exposure_us=us)

    def _exp_slid(self, pos):
        if self._updating or self.cam is None:
            return
        cur = next((x for x in EXP_RANGES if x[0] == self.exp_range.get()), EXP_RANGES[1])
        _, lo, hi = cur
        us = int(round(lo * (hi / lo) ** (float(pos) / 1000.0)))
        lo2, hi2 = self._exp_limits()
        us = max(lo2, min(hi2, us))
        self.exp_var.set(("%.3f" % (us / 1000.0)).rstrip("0").rstrip("."))
        self.queue_set(exposure_us=us)

    def _exp_range_changed(self):
        cur = next((x for x in EXP_RANGES if x[0] == self.exp_range.get()), EXP_RANGES[1])
        us = self.settings.get("exposure_us", 10000)
        if not (cur[1] <= us <= cur[2]):
            us = max(cur[1], min(cur[2], us))
            self.queue_set(exposure_us=us)
        self._show_exposure(us, move_range=False)
        self._save_prefs()

    def _exp_auto_changed(self):
        self._apply_auto_states()
        self.queue_set(auto_exposure=self.exp_auto.get())

    def _gain_typed(self):
        if self._updating or self.cam is None or self.gain_auto.get():
            return
        g = self.controls.get("gain", {"min": 0, "max": 600})
        try:
            v = max(g["min"], min(g["max"], int(float(self.gain_var.get()))))
        except ValueError:
            self.gain_var.set(str(self.settings.get("gain", 0)))
            return
        self.gain_var.set(str(v))
        self._updating = True
        self.gain_scale.set(v)
        self._updating = False
        if v != self.settings.get("gain"):
            self.queue_set(gain=v)

    def _gain_slid(self, pos):
        if self._updating or self.cam is None:
            return
        v = int(round(float(pos)))
        self.gain_var.set(str(v))
        self.queue_set(gain=v)

    def _gain_auto_changed(self):
        self._apply_auto_states()
        self.queue_set(auto_gain=self.gain_auto.get())

    def _adv_typed(self, key):
        if self._updating or self.cam is None:
            return
        v, _ = self.adv_vars[key]
        try:
            val = int(float(v.get()))
        except ValueError:
            cur = self.settings.get(key)
            v.set("" if cur is None else str(cur))
            return
        c = self.controls.get(key)
        if c:
            val = max(c["min"], min(c["max"], val))
            v.set(str(val))
        if val != self.settings.get(key):
            self.queue_set(**{key: val})

    # ------------------------------------------------------------ vista
    def _toggle_zoom(self):
        self.zoom.set("1:1" if self.zoom.get() == "Ajustar" else "Ajustar")
        self._pan = [0, 0]
        self._redraw()

    def _drag_start(self, e):
        self._drag = (e.x, e.y, self._pan[0], self._pan[1])

    def _drag_move(self, e):
        if self._drag is None or self.zoom.get() != "1:1":
            return
        x0, y0, px, py = self._drag
        self._pan = [px - (e.x - x0), py - (e.y - y0)]
        self._redraw()

    def _redraw(self):
        if self._shown is not None:
            self._render(self._shown, force_hist=False)
        self._place_cross()

    def _place_cross(self):
        cw, ch = self.canvas.winfo_width(), self.canvas.winfo_height()
        st = "normal" if self.cross_var.get() else "hidden"
        self.canvas.coords(self.cross[0], cw / 2, 0, cw / 2, ch)
        self.canvas.coords(self.cross[1], 0, ch / 2, cw, ch / 2)
        for c in self.cross:
            self.canvas.itemconfigure(c, state=st)
            self.canvas.tag_raise(c)
        self.canvas.tag_raise(self.overlay)

    def _lut(self, sample, raw16):
        """Tabla para pasar a 8 bits: lineal o estirada entre percentiles."""
        n = 4096 if raw16 else 256
        if self.stretch.get() == "Automático":
            lo, hi = np.percentile(sample, (0.5, 99.8))
            if hi - lo < 2:
                hi = lo + 2
        else:
            lo, hi = 0.0, n - 1.0
        x = np.arange(n, dtype=np.float32)
        return np.clip((x - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)

    def _render(self, f, force_hist=True):
        cw, ch = max(10, self.canvas.winfo_width()), max(10, self.canvas.winfo_height())
        raw16 = f.image.dtype != np.uint8
        img = f.image
        h, w = img.shape[:2]
        if self.zoom.get() == "1:1":
            vw, vh = min(w, cw), min(h, ch)
            x0 = int(max(0, min(w - vw, (w - vw) // 2 + self._pan[0])))
            y0 = int(max(0, min(h - vh, (h - vh) // 2 + self._pan[1])))
            sub = img[y0:y0 + vh, x0:x0 + vw]
            step, zoom = 1, 1
        else:
            step = max(1, int(math.ceil(max(w / cw, h / ch))))
            sub = img[::step, ::step]
            x0 = y0 = 0
            zoom = max(1, int(min(cw / sub.shape[1], ch / sub.shape[0]))) if step == 1 else 1
        if raw16:
            sub = sub >> 4          # 12 bits reales (se recorta antes para no procesar de mas)
        sample = sub[::4, ::4]
        lut = self._lut(sample, raw16)
        disp = lut[sub]
        sh, sw = disp.shape
        data = b"P5\n%d %d\n255\n" % (sw, sh) + np.ascontiguousarray(disp).tobytes()
        photo = tk.PhotoImage(data=data, format="PPM")
        if zoom > 1:
            photo = photo.zoom(zoom)
        ox, oy = (cw - sw * zoom) // 2, (ch - sh * zoom) // 2
        if self._img_item is None:
            self._img_item = self.canvas.create_image(ox, oy, image=photo, anchor="nw")
        else:
            self.canvas.coords(self._img_item, ox, oy)
            self.canvas.itemconfigure(self._img_item, image=photo)
        self._photo = photo
        self.canvas.tag_lower(self._img_item)
        self._view = (x0, y0, step, zoom, ox, oy, sw, sh)
        self._place_cross()
        if force_hist:
            self._hist_n += 1
            if self._hist_n % 2 == 0:
                self._draw_hist(f)

    def _draw_hist(self, f):
        raw16 = f.image.dtype != np.uint8
        s = f.image[::2, ::2]
        b = (s >> 8) if raw16 else s
        counts = np.bincount(b.ravel(), minlength=256)[:256].astype(np.float64)
        shift = 4 if raw16 else 0
        mn, mx = int(f.image.min()) >> shift, int(f.image.max()) >> shift
        avg = float((s >> 4).mean()) if raw16 else float(s.mean())
        top = (65520 if raw16 else 255)
        sat = float((s >= top).mean() * 100)
        cv = self.hist
        cv.delete("all")
        W, H = int(cv["width"]), int(cv["height"])
        y = np.log1p(counts) if self.hist_log.get() else counts
        y = y / (y.max() or 1.0)
        pts = [0, H]
        for i in range(256):
            pts += [i * (W - 1) / 255.0, H - 2 - y[i] * (H - 6)]
        pts += [W - 1, H]
        cv.create_polygon(pts, fill="#3A5A78", outline=C_ACC)
        for q in (64, 128, 192):
            cv.create_line(q * W / 256, 0, q * W / 256, H, fill=C_LINE)
        self.hist_lbl.configure(text="mín %d  máx %d  prom %.0f  sat %.1f%%" % (mn, mx, avg, sat),
                                fg=C_ERR if sat > 1 else C_TEXT)

    def _on_motion(self, e):
        f, v = self._shown, self._view
        if f is None or v is None:
            return
        x0, y0, step, zoom, ox, oy, sw, sh = v
        cx, cy = (e.x - ox) // zoom, (e.y - oy) // zoom
        if not (0 <= cx < sw and 0 <= cy < sh):
            self.pix_lbl.configure(text="")
            return
        ix, iy = x0 + cx * step, y0 + cy * step
        val = int(f.image[iy, ix]) >> (4 if f.image.dtype != np.uint8 else 0)
        self.pix_lbl.configure(text="x %d  y %d  valor %d" % (ix, iy, val))

    # ------------------------------------------------------------ captura
    def _out_path(self, ext):
        d = Path(self.folder.get() or DEFAULT_FOLDER)
        d.mkdir(parents=True, exist_ok=True)
        t = time.time()
        return d / ("asicam_%s_%03d.%s" % (time.strftime("%Y%m%d_%H%M%S", time.localtime(t)),
                                            int(t * 1000) % 1000, ext))

    def snapshot(self):
        f = self.cam.latest() if self.cam else None
        if f is None:
            self.message("Todavía no llegó ningún cuadro.", C_WARN)
            return None
        fmt = self.photo_fmt.get()
        try:
            path = self._out_path("fits" if fmt == "FITS" else "pgm")
            if fmt == "FITS":
                save_fits(str(path), f, {"INSTRUME": (self.info or {}).get("camera", {}).get("name", "")})
            else:
                save_pgm(str(path), f)
        except OSError as e:
            self.message("No pude guardar la foto: %s" % e, C_ERR)
            return None
        self.message("Foto guardada: %s" % path, C_OK)
        self._save_prefs()
        return path

    def toggle_record(self):
        if self.recorder.active:
            self.recorder.stop()
            return
        if self.cam is None:
            return
        _, nf, ns = next((x for x in REC_LIMITS if x[0] == self.rec_limit.get()), REC_LIMITS[0])
        try:
            path = self._out_path("ser")
        except OSError as e:
            self.message("No pude crear la carpeta: %s" % e, C_ERR)
            return
        self.recorder.start(str(path), nf, ns, (self.info or {}).get("camera", {}).get("name", ""))
        self.rec_btn.configure(text="■ Detener", bg=C_REC)
        for w in self.geom_widgets:
            w.configure(state="disabled")
        self.message("Grabando en %s" % path, C_WARN)
        self._save_prefs()

    def _record_tick(self):
        rc = self.recorder
        if rc.active:
            self.rec_lbl.configure(text="● %d cuadros · %.1f s · %d perdidos al grabar" % (
                rc.frames, rc.elapsed(), rc.lost), fg=C_REC)
        elif rc.done:
            rc.done = False
            self.rec_btn.configure(text="● Grabar .ser", bg=C_PANEL2)
            for w in self.geom_widgets:
                w.configure(state="readonly" if self.cam else "disabled")
            txt = "Video: %d cuadros, %d perdidos al grabar → %s" % (rc.frames, rc.lost, rc.path)
            self.rec_lbl.configure(text="", fg=C_MUTED)
            self.message(txt if not rc.error else "%s (%s)" % (txt, rc.error), C_ERR if rc.error else C_OK)

    # ------------------------------------------------------------ ciclo
    def _tick(self):
        try:
            self.worker.pump()
            cam = self.cam
            if cam is not None:
                f = cam.latest()
                if f is not None and f is not self._shown:
                    self._shown = f
                    self._render(f)
                    now = time.monotonic()
                    self._disp_times.append(now)
                    if len(self._disp_times) > 40:
                        del self._disp_times[:20]
                    if self.exp_auto.get():
                        self._show_exposure(f.exposure_us)
                    if self.gain_auto.get():
                        self.gain_var.set(str(f.gain))
                        self._updating = True
                        self.gain_scale.set(f.gain)
                        self._updating = False
                self._status_tick()
            self._record_tick()
            if self.test is not None:
                self.test.step()
        finally:
            self.root.after(TICK_MS, self._tick)

    def disp_fps(self):
        t = [x for x in self._disp_times if time.monotonic() - x < 3]
        return (len(t) - 1) / (t[-1] - t[0]) if len(t) > 1 and t[-1] > t[0] else 0.0

    def _status_tick(self):
        now = time.monotonic()
        cam = self.cam
        if not self._status_busy and now - self._status_t > 1.0:
            self._status_busy = True
            self._status_t = now

            def done(st):
                self._status_busy = False
                if isinstance(st, dict):
                    self._server_status = st
            self.worker.submit(cam.status, done, done)
        if now - self._label_t < 0.25:
            return
        self._label_t = now
        st = cam.stats()
        if now - self._rate_t >= 1.0:
            db = st["bytes"] - self._rate_b
            self._mbps = max(0.0, db * 8 / (now - self._rate_t) / 1e6) if self._rate_t else 0.0
            self._rate_t, self._rate_b = now, st["bytes"]
        ss = self._server_status
        f = self._shown
        temp = f.temperature_c if f is not None else None
        self.status_lbl.configure(text=(
            "recibidos %.1f fps · vista %.1f fps · %.0f Mbit/s · huecos %d · descartes SDK %s · "
            "reinicios %s · reconexiones %d · servidor: %s") % (
                st["fps"], self.disp_fps(), self._mbps, st["gaps"], ss.get("dropped_sdk", "-"),
                ss.get("restarts", "-"), st["reconnects"], ss.get("state", "-")))
        if f is not None:
            self.canvas.itemconfigure(self.overlay, text="#%d  %d×%d %s bin%d  exp %s%s  gan %d%s  %s" % (
                f.seq, f.width, f.height, f.image_type, f.bin, fmt_exp(f.exposure_us),
                " (auto)" if f.auto_exposure else "", f.gain, " (auto)" if f.auto_gain else "",
                ("%.1f °C" % temp) if temp is not None else ""))
            self.cam_det.configure(text=self._cam_det_base + (("  ·  %.1f °C" % temp) if temp is not None else ""))


# ----------------------------------------------------------------------------
# Prueba automatica (--selftest): maneja la interfaz como un usuario
# ----------------------------------------------------------------------------

class SelfTest:
    def __init__(self, gui):
        self.g = gui
        self.results = []
        self.steps = self._script()
        self.wait_until = None
        self.deadline = None
        self.finished = False
        self.original = dict(gui.settings)
        print("SELFTEST: conectado a %s (%s)" % (gui.host_var.get(), gui.info["camera"]["name"]), flush=True)

    def check(self, name, ok, detail=""):
        self.results.append((name, bool(ok)))
        print("  [%s] %s%s" % ("OK " if ok else "FALLA", name, (" - " + detail) if detail else ""), flush=True)

    def fail_now(self, why):
        self.check(why, False)
        self._end()

    def _frames_until(self, pred, timeout):
        """Paso: espera un cuadro mostrado que cumpla pred."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            f = self.g._shown
            if f is not None and pred(f):
                return f, time.monotonic() - t0
            yield
        yield None
        return None, timeout

    def _sleep(self, secs):
        t0 = time.monotonic()
        while time.monotonic() - t0 < secs:
            yield

    def _script(self):
        g = self.g
        yield from self._sleep(3)
        f = g._shown
        self.check("se ve video", f is not None, repr(f))
        self.check("vista fluida", g.disp_fps() > 5, "vista %.1f fps, recibidos %.1f fps" % (
            g.disp_fps(), g.cam.stats()["fps"]))
        self.check("histograma dibujado", bool(g.hist.find_all()), g.hist_lbl.cget("text"))

        # exposicion escrita a mano
        g.exp_var.set("5")
        g._exp_typed()
        r = yield from self._frames_until(lambda x: x.exposure_us == 5000, 6)
        self.check("exposición 5 ms desde el campo", r[0] is not None)
        # deslizador de ganancia
        g.gain_scale.set(120)
        g._gain_slid(120)
        r = yield from self._frames_until(lambda x: x.gain == 120, 6)
        self.check("ganancia 120 desde el deslizador", r[0] is not None)
        # exposicion automatica
        g.exp_auto.set(True)
        g._exp_auto_changed()
        r = yield from self._frames_until(lambda x: x.auto_exposure, 6)
        self.check("exposición automática", r[0] is not None and str(g.exp_entry.cget("state")) == "disabled",
                   "el campo sigue al valor real: %s ms" % g.exp_var.get())
        g.exp_auto.set(False)
        g._exp_auto_changed()
        yield from self._sleep(0.5)
        g.exp_var.set("10")
        g._exp_typed()
        g.gain_var.set("0")
        g._gain_typed()
        r = yield from self._frames_until(lambda x: not x.auto_exposure and x.exposure_us == 10000 and x.gain == 0, 6)
        self.check("vuelve a manual 10 ms, ganancia 0", r[0] is not None)

        # formato, bin y resolucion desde los desplegables
        for desc, fmt, b, res, shape in (("RAW16", "RAW16", "Bin1", None, (1080, 1920)),
                                         ("Bin2", "RAW8", "Bin2", None, (540, 960)),
                                         ("1280×720", "RAW8", "Bin1", "1280×720", (720, 1280))):
            g.fmt_var.set(fmt)
            g.bin_var.set(b)
            g._fill_resolutions()
            g.res_var.set(res if res else next(iter(g._res_map)))
            g._geometry_changed()
            r = yield from self._frames_until(lambda x, s=shape, t=fmt: x.image.shape == s and x.image_type == t, 10)
            self.check("cambio a %s" % desc, r[0] is not None, "%.2f s" % r[1])
            yield from self._sleep(0.6)
        g.fmt_var.set("RAW8")
        g.bin_var.set("Bin1")
        g._fill_resolutions()
        g.res_var.set(next(iter(g._res_map)))
        g._geometry_changed()
        r = yield from self._frames_until(lambda x: x.image.shape == (1080, 1920) and x.image_type == "RAW8", 10)
        self.check("vuelve a 1080p RAW8", r[0] is not None)

        # vista
        g.zoom.set("1:1")
        g.cross_var.set(True)
        g._redraw()
        yield from self._sleep(1)
        self.check("zoom 1:1 y cruz", g._view is not None and g._view[2] == 1 and
                   g.canvas.itemcget(g.cross[0], "state") == "normal")
        g.zoom.set("Ajustar")
        g._redraw()

        # foto y video
        folder = tempfile.mkdtemp(prefix="asicam-gui-")
        g.folder.set(folder)
        g.photo_fmt.set("FITS")
        p = g.snapshot()
        self.check("foto FITS", p is not None and p.exists() and p.stat().st_size % 2880 == 0,
                   "%s (%d bytes)" % (p, p.stat().st_size) if p else "")
        g.rec_limit.set("10 s")
        g.toggle_record()
        yield from self._sleep(3)
        g.toggle_record()
        t0 = time.monotonic()
        while g.recorder.active or g.recorder.thread.is_alive():
            if time.monotonic() - t0 > 10:
                break
            yield
        rc = g.recorder
        hdr = read_ser_header(rc.path) if rc.path and os.path.exists(rc.path) else None
        ok = hdr is not None and hdr["id"] == b"LUCAM-RECORDER" and hdr["frames"] == rc.frames > 20 and \
            hdr["size"] == 178 + hdr["frames"] * hdr["width"] * hdr["height"] * hdr["depth"] // 8 + 8 * hdr["frames"]
        self.check("video .ser de 3 s", ok, "%d cuadros, %d perdidos, %s" % (rc.frames, rc.lost, hdr))
        if self.g.hold:
            print("SELFTEST: ventana abierta %.0f s para la captura de pantalla" % self.g.hold, flush=True)
            yield from self._sleep(self.g.hold)

    def step(self):
        if self.finished:
            return
        try:
            next(self.steps)
        except StopIteration:
            self._end()

    def _end(self):
        if self.finished:
            return
        self.finished = True
        g = self.g
        try:
            if g.cam is not None and self.original:
                g.cam.set(**self.original)
        except AsiCamError:
            pass
        bad = [n for n, ok in self.results if not ok]
        print("\nRESUMEN: %d pruebas, %d fallas%s" % (len(self.results), len(bad),
                                                    (": " + "; ".join(bad)) if bad else ""), flush=True)
        g.root.after(100, g.on_close)
        g.exit_code = 1 if bad else 0


def start_simulator():
    srv = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pi", "asicam_server.py")
    cfg = os.path.join(tempfile.mkdtemp(prefix="asicam-sim-"), "settings.json")
    p = subprocess.Popen([sys.executable, srv, "--simulate", "--bind", "127.0.0.1", "--config", cfg],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    return p


def main(argv=None):
    ap = argparse.ArgumentParser(description="Interfaz de la camara asicam")
    ap.add_argument("--host", help="IP del servidor asicam")
    ap.add_argument("--simulate", action="store_true", help="levanta un servidor simulado local y se conecta")
    ap.add_argument("--selftest", action="store_true", help="prueba automatica de la interfaz y sale")
    ap.add_argument("--hold", type=float, default=0.0, help="con --selftest: segundos de espera al final")
    a = ap.parse_args(argv)
    # en el ejecutable sin consola no hay stdout/stderr: print fallaria
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    sim = start_simulator() if a.simulate else None
    host = "127.0.0.1" if a.simulate else a.host
    root = tk.Tk()
    gui = AsiCamGui(root, host=host, selftest=a.selftest, hold=a.hold)
    if a.selftest:
        def watchdog():
            if gui.test is None or not gui.test.finished:
                print("SELFTEST: tiempo agotado", flush=True)
                gui.exit_code = 1
                gui.on_close()
        root.after(int((180 + a.hold) * 1000), watchdog)
    try:
        root.mainloop()
    finally:
        if sim is not None:
            sim.terminate()
            sim.wait(10)
    sys.exit(gui.exit_code)


if __name__ == "__main__":
    main()
