#!/usr/bin/env python3
"""Interfaz de control de la montura PTZ ufomount.

Habla con el firmware ptztest v3 (Arduino UNO, 115200 baudios).
Eje X = pan 270 grados (D9) - Eje Y = tilt 180 grados (D10).

Uso:
    python scripts/ptz_gui.py                  # abre la interfaz
    python scripts/ptz_gui.py --port COM7      # otro puerto
    python scripts/ptz_gui.py --check          # prueba de conexion sin GUI

La posicion mostrada es la ESTIMADA (lazo abierto): el firmware sabe
donde comando al servo; el servo no tiene salida de telemetria real.
"""

import argparse
import math
import queue
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    print("Falta pyserial:  python -m pip install pyserial")
    sys.exit(1)

# ----------------------------- configuracion -----------------------------
PORT_DEFAULT = "COM5"
BAUD = 115200
BOOT_WAIT_S = 2.5          # el UNO se resetea al abrir el puerto

X_MIN, X_MAX = 500, 2500   # pulso (us)
Y_MIN, Y_MAX = 500, 2500
X_DEG_TOTAL = 270.0        # recorrido fisico del eje X
Y_DEG_TOTAL = 180.0
INVERT_X = False           # invertir sentido visual de un eje si hace falta
INVERT_Y = False

X_US_PER_DEG = (X_MAX - X_MIN) / X_DEG_TOTAL   # ~7.41
Y_US_PER_DEG = (Y_MAX - Y_MIN) / Y_DEG_TOTAL   # ~11.11

SPEED_PRESETS = [("Fino", 2.0), ("Normal", 8.0), ("Avion", 25.0)]  # grados/s
SPEED_MIN, SPEED_MAX = 0.5, 40.0

STATUS_POLL_S = 0.2
CMD_SETTLE_S = 0.7         # ventana en que un comando aun no se refleja en STATUS
WDT_MS = 1000              # watchdog de enlace en el firmware

STATUS_RE = re.compile(
    r"attX=(\d) attY=(\d) X=(\d+)/(\d+) \[(\d+)-(\d+)\]"
    r" Y=(\d+)/(\d+) \[(\d+)-(\d+)\] rateX=(\d+) rateY=(\d+) idle=(\d+)"
)

# ------------------------------- colores ---------------------------------
C_BG = "#14191F"
C_PANEL = "#1B222A"
C_PANEL2 = "#232C34"
C_TEXT = "#E4E9ED"
C_MUTED = "#92A0AC"
C_X = "#F08A2E"        # pan  (naranja)
C_Y = "#64B5F6"        # tilt (azul)
C_OK = "#7FC698"
C_ERR = "#E57373"
C_WARN = "#D9A93F"
C_LINE = "#2A343D"


def us_to_deg(us, axis):
    if axis == "X":
        deg = (us - (X_MIN + X_MAX) / 2) / X_US_PER_DEG
        return -deg if INVERT_X else deg
    deg = (us - (Y_MIN + Y_MAX) / 2) / Y_US_PER_DEG
    return -deg if INVERT_Y else deg


def deg_to_us(deg, axis):
    if axis == "X":
        if INVERT_X:
            deg = -deg
        return int(round((X_MIN + X_MAX) / 2 + deg * X_US_PER_DEG))
    if INVERT_Y:
        deg = -deg
    return int(round((Y_MIN + Y_MAX) / 2 + deg * Y_US_PER_DEG))


class SerialLink:
    """Conexion serie con hilo lector generacional.

    Cada open() crea un hilo con SU PROPIO handle y SU PROPIO Event de
    parada: un hilo viejo nunca puede observar (ni matar) una conexion
    nueva, aunque el usuario conecte/desconecte rapidamente.
    """

    def __init__(self, rxq):
        self.rxq = rxq
        self.ser = None
        self._stop = None

    @property
    def connected(self):
        return self.ser is not None and self.ser.is_open

    def open(self, port):
        ser = serial.Serial(port, BAUD, timeout=0.2)
        stop = threading.Event()
        self.ser = ser
        self._stop = stop
        threading.Thread(target=self._reader, args=(ser, stop),
                         daemon=True).start()

    def close(self):
        if self._stop is not None:
            self._stop.set()
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def write_line(self, text):
        self.ser.write((text + "\n").encode("ascii"))

    def _reader(self, ser, stop):
        while not stop.is_set():
            try:
                raw = ser.readline()
            except Exception:
                if not stop.is_set():
                    self.rxq.put(("lost", ""))
                return
            if raw:
                line = raw.decode("ascii", errors="replace").strip()
                if line:
                    self.rxq.put(("rx", line))


class PtzGui:
    def __init__(self, root, port=PORT_DEFAULT):
        self.root = root
        self.rxq = queue.Queue()
        self.link = SerialLink(self.rxq)

        self.cur = {"X": 1500, "Y": 1500}
        self.tgt = {"X": 1500, "Y": 1500}
        self.att = {"X": False, "Y": False}
        self.jogging = {"X": 0, "Y": 0}          # -1, 0, +1
        self.jog_src = {"X": set(), "Y": set()}  # duenos del jog: mouse/key
        self.move_sent = {"X": 0.0, "Y": 0.0}    # ts del ultimo comando de mov.
        self.keys_down = set()
        self.last_tx = 0.0
        self.awaiting_reply = False
        self.pong_ok = False
        self._speed_job = None
        self._handshake_job = None
        self._pending_y = None                   # (y_us, ts) goto secuencial

        self._build_ui(port)
        self._tick()

    # --------------------------- construccion UI ---------------------------
    def _build_ui(self, port):
        r = self.root
        r.title("UFOMOUNT PTZ")
        r.configure(bg=C_BG)
        r.geometry("980x640")
        r.minsize(760, 520)

        style = ttk.Style(r)
        style.theme_use("clam")
        style.configure("TFrame", background=C_BG)
        style.configure("Panel.TFrame", background=C_PANEL)
        style.configure("TLabel", background=C_BG, foreground=C_TEXT)
        style.configure("Panel.TLabel", background=C_PANEL, foreground=C_TEXT)
        style.configure("Muted.TLabel", background=C_PANEL, foreground=C_MUTED)
        style.configure("TButton", background=C_PANEL2, foreground=C_TEXT,
                        borderwidth=0, focuscolor=C_PANEL2, padding=6)
        style.map("TButton", background=[("active", C_LINE)])
        style.configure("TCheckbutton", background=C_PANEL, foreground=C_MUTED)
        style.map("TCheckbutton", background=[("active", C_PANEL)])
        style.configure("TCombobox", fieldbackground=C_PANEL2,
                        background=C_PANEL2, foreground=C_TEXT)
        style.configure("Horizontal.TScale", background=C_PANEL)

        r.columnconfigure(0, weight=0)
        r.columnconfigure(1, weight=1)
        r.rowconfigure(1, weight=1)

        # ---- barra superior: conexion ----
        top = ttk.Frame(r, style="Panel.TFrame", padding=8)
        top.grid(row=0, column=0, columnspan=2, sticky="ew", padx=8, pady=(8, 4))
        top.columnconfigure(5, weight=1)

        ttk.Label(top, text="UFOMOUNT PTZ", style="Panel.TLabel",
                  font=("Segoe UI", 13, "bold")).grid(row=0, column=0, padx=(2, 14))
        ttk.Label(top, text="Puerto:", style="Muted.TLabel").grid(row=0, column=1)
        self.port_var = tk.StringVar(value=port)
        self.port_cb = ttk.Combobox(top, textvariable=self.port_var, width=10,
                                    values=self._ports())
        self.port_cb.grid(row=0, column=2, padx=4)
        ttk.Button(top, text="↻", width=3,
                   command=lambda: self.port_cb.configure(values=self._ports())
                   ).grid(row=0, column=3)
        self.conn_btn = ttk.Button(top, text="Conectar", command=self.toggle_conn)
        self.conn_btn.grid(row=0, column=4, padx=8)
        self.conn_lbl = tk.Label(top, text="desconectado", bg=C_PANEL2,
                                 fg=C_MUTED, padx=10, pady=2)
        self.conn_lbl.grid(row=0, column=5, sticky="e", padx=2)

        # ---- columna izquierda: controles ----
        left = ttk.Frame(r, style="Panel.TFrame", padding=10)
        left.grid(row=1, column=0, sticky="nsw", padx=(8, 4), pady=4)

        ttk.Label(left, text="CONTROL", style="Muted.TLabel",
                  font=("Segoe UI", 8, "bold")).pack(anchor="w")

        pad = tk.Frame(left, bg=C_PANEL)
        pad.pack(pady=8)
        mk = dict(width=4, height=2, bd=0, font=("Segoe UI", 12, "bold"),
                  activebackground=C_LINE, takefocus=0)
        self.btn_up = tk.Button(pad, text="▲", bg=C_PANEL2, fg=C_Y, **mk)
        self.btn_dn = tk.Button(pad, text="▼", bg=C_PANEL2, fg=C_Y, **mk)
        self.btn_lf = tk.Button(pad, text="◀", bg=C_PANEL2, fg=C_X, **mk)
        self.btn_rt = tk.Button(pad, text="▶", bg=C_PANEL2, fg=C_X, **mk)
        stop = tk.Button(pad, text="STOP", bg="#5A2A2A", fg=C_TEXT, bd=0,
                         font=("Segoe UI", 9, "bold"), width=5, height=2,
                         activebackground="#7A3A3A", command=self.stop_all)
        self.btn_up.grid(row=0, column=1, padx=3, pady=3)
        self.btn_lf.grid(row=1, column=0, padx=3, pady=3)
        stop.grid(row=1, column=1, padx=3, pady=3)
        self.btn_rt.grid(row=1, column=2, padx=3, pady=3)
        self.btn_dn.grid(row=2, column=1, padx=3, pady=3)

        for btn, axis, d in ((self.btn_lf, "X", -1), (self.btn_rt, "X", +1),
                             (self.btn_up, "Y", +1), (self.btn_dn, "Y", -1)):
            btn.bind("<ButtonPress-1>",
                     lambda e, a=axis, s=d: self.jog_start(a, s, "mouse"))
            btn.bind("<ButtonRelease-1>",
                     lambda e, a=axis: self.jog_stop(a, "mouse"))

        ttk.Label(left, text="(tambien flechas del teclado; espacio = STOP)",
                  style="Muted.TLabel").pack()

        # velocidad
        ttk.Label(left, text="VELOCIDAD", style="Muted.TLabel",
                  font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(14, 0))
        self.speed_var = tk.DoubleVar(value=8.0)
        self.speed_lbl = ttk.Label(left, text="8.0 °/s", style="Panel.TLabel",
                                   font=("Segoe UI", 11, "bold"))
        self.speed_lbl.pack(anchor="w")
        sc = ttk.Scale(left, from_=SPEED_MIN, to=SPEED_MAX, orient="horizontal",
                       variable=self.speed_var, command=self._speed_changed,
                       length=190)
        sc.pack(fill="x", pady=2)
        prow = tk.Frame(left, bg=C_PANEL)
        prow.pack(fill="x", pady=2)
        for name, val in SPEED_PRESETS:
            b = tk.Button(prow, text=f"{name}\n{val:g}°/s", bd=0,
                          bg=C_PANEL2, fg=C_TEXT, font=("Segoe UI", 8),
                          activebackground=C_LINE, width=7, takefocus=0,
                          command=lambda v=val: self.set_speed(v))
            b.pack(side="left", padx=2, expand=True, fill="x")

        # ir a posicion
        ttk.Label(left, text="IR A (grados)", style="Muted.TLabel",
                  font=("Segoe UI", 8, "bold")).pack(anchor="w", pady=(14, 2))
        grow = tk.Frame(left, bg=C_PANEL)
        grow.pack(fill="x")
        self.goto_x = tk.Spinbox(grow, from_=-X_DEG_TOTAL / 2, to=X_DEG_TOTAL / 2,
                                 increment=1, width=6, bg=C_PANEL2, fg=C_X,
                                 buttonbackground=C_PANEL2, bd=0,
                                 insertbackground=C_TEXT)
        self.goto_y = tk.Spinbox(grow, from_=-Y_DEG_TOTAL / 2, to=Y_DEG_TOTAL / 2,
                                 increment=1, width=6, bg=C_PANEL2, fg=C_Y,
                                 buttonbackground=C_PANEL2, bd=0,
                                 insertbackground=C_TEXT)
        self.goto_x.delete(0, "end"); self.goto_x.insert(0, "0")
        self.goto_y.delete(0, "end"); self.goto_y.insert(0, "0")
        tk.Label(grow, text="X", bg=C_PANEL, fg=C_X).pack(side="left")
        self.goto_x.pack(side="left", padx=(2, 8))
        tk.Label(grow, text="Y", bg=C_PANEL, fg=C_Y).pack(side="left")
        self.goto_y.pack(side="left", padx=2)
        grow2 = tk.Frame(left, bg=C_PANEL)
        grow2.pack(fill="x", pady=4)
        ttk.Button(grow2, text="Ir", command=self.goto).pack(
            side="left", expand=True, fill="x", padx=(0, 2))
        ttk.Button(grow2, text="Centrar", command=self.center).pack(
            side="left", expand=True, fill="x", padx=(2, 0))

        # opciones
        self.seq_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(left, text="Mover ejes de a uno (fuente justa)",
                        variable=self.seq_var).pack(anchor="w", pady=(12, 0))
        self.dbg_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text="Ver polls STATUS en el log",
                        variable=self.dbg_var).pack(anchor="w")

        # ---- columna derecha: posicion + log ----
        right = ttk.Frame(r, style="TFrame")
        right.grid(row=1, column=1, sticky="nsew", padx=(4, 8), pady=4)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)

        pos = ttk.Frame(right, style="Panel.TFrame", padding=10)
        pos.grid(row=0, column=0, sticky="ew")
        pos.columnconfigure(0, weight=1)
        pos.columnconfigure(1, weight=1)

        tk.Label(pos, text="PAN X · vista superior", bg=C_PANEL, fg=C_X,
                 font=("Segoe UI", 9, "bold")).grid(row=0, column=0)
        tk.Label(pos, text="TILT Y · vista lateral", bg=C_PANEL, fg=C_Y,
                 font=("Segoe UI", 9, "bold")).grid(row=0, column=1)
        self.dial_x = tk.Canvas(pos, height=190, bg=C_PANEL,
                                highlightthickness=0)
        self.dial_y = tk.Canvas(pos, height=190, bg=C_PANEL,
                                highlightthickness=0)
        self.dial_x.grid(row=1, column=0, sticky="ew", padx=(0, 6))
        self.dial_y.grid(row=1, column=1, sticky="ew", padx=(6, 0))

        self.deg_x = tk.Label(pos, text="+0.0°", bg=C_PANEL, fg=C_X,
                              font=("Consolas", 20, "bold"))
        self.deg_y = tk.Label(pos, text="+0.0°", bg=C_PANEL, fg=C_Y,
                              font=("Consolas", 20, "bold"))
        self.us_x = tk.Label(pos, text="1500 us · reposo", bg=C_PANEL,
                             fg=C_MUTED, font=("Consolas", 9))
        self.us_y = tk.Label(pos, text="1500 us · reposo", bg=C_PANEL,
                             fg=C_MUTED, font=("Consolas", 9))
        self.deg_x.grid(row=2, column=0)
        self.deg_y.grid(row=2, column=1)
        self.us_x.grid(row=3, column=0)
        self.us_y.grid(row=3, column=1)

        for c in (self.dial_x, self.dial_y):
            c.bind("<Configure>", lambda e: self._redraw_dials())
        tk.Label(pos, text="posicion estimada (lazo abierto) · aguja = camara, "
                 "cono = campo de vision, linea punteada = objetivo",
                 bg=C_PANEL, fg=C_MUTED, font=("Segoe UI", 8)).grid(
                     row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

        logf = ttk.Frame(right, style="Panel.TFrame", padding=(8, 6))
        logf.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        logf.columnconfigure(0, weight=1)
        logf.rowconfigure(1, weight=1)
        ttk.Label(logf, text="LOG", style="Muted.TLabel",
                  font=("Segoe UI", 8, "bold")).grid(row=0, column=0, sticky="w")
        self.log = tk.Text(logf, bg=C_BG, fg=C_TEXT, bd=0, wrap="none",
                           font=("Consolas", 9), state="disabled")
        sb = ttk.Scrollbar(logf, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        self.log.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        sb.grid(row=1, column=1, sticky="ns")
        for tag, color in (("tx", "#8FB4D8"), ("rx", C_OK), ("err", C_ERR),
                           ("warn", C_WARN), ("info", C_MUTED), ("dim", "#5A6874")):
            self.log.tag_configure(tag, foreground=color)

        # teclado y foco
        r.bind("<KeyPress>", self._key_press)
        r.bind("<KeyRelease>", self._key_release)
        r.bind("<FocusOut>",
               lambda e: self.root.after_idle(self._app_focus_check))
        r.protocol("WM_DELETE_WINDOW", self._on_close)

    def _ports(self):
        return [p.device for p in list_ports.comports()] or [PORT_DEFAULT]

    # ------------------------------- log -----------------------------------
    def log_line(self, text, tag="info"):
        ts = time.strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"{ts}  {text}\n", tag)
        if int(self.log.index("end-1c").split(".")[0]) > 500:
            self.log.delete("1.0", "2.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ----------------------------- conexion ---------------------------------
    def toggle_conn(self):
        if self.link.connected:
            # detener antes de soltar el enlace: no abandonar la montura en
            # movimiento (el watchdog del firmware es la segunda red).
            try:
                self.link.write_line("STOP")
                self.log_line("> STOP (antes de desconectar)", "tx")
            except Exception:
                pass
            self.disconnect("desconectado por el usuario")
            return
        port = self.port_var.get().strip()
        try:
            self.link.open(port)
        except Exception as exc:
            self.log_line(f"ERROR abriendo {port}: {exc}", "err")
            return
        self._reset_local_state()
        self.conn_lbl.configure(text=f"conectando a {port}...", fg=C_WARN)
        self.conn_btn.configure(text="Desconectar")
        self.log_line(f"puerto {port} abierto, esperando arranque del UNO "
                      f"({BOOT_WAIT_S:.0f} s)...", "info")
        self._handshake_job = self.root.after(int(BOOT_WAIT_S * 1000),
                                              self._handshake)

    def _reset_local_state(self):
        self.cur = {"X": 1500, "Y": 1500}
        self.tgt = {"X": 1500, "Y": 1500}
        self.att = {"X": False, "Y": False}
        self.jogging = {"X": 0, "Y": 0}
        self.jog_src = {"X": set(), "Y": set()}
        self.move_sent = {"X": 0.0, "Y": 0.0}
        self._pending_y = None
        self.pong_ok = False
        self.awaiting_reply = False

    def _handshake(self):
        self._handshake_job = None
        if not self.link.connected:
            return
        self.pong_ok = False
        self.send("PING")
        self.send(f"WDT {WDT_MS}", log=False)
        self.send(f"RATEX {max(5, int(self.speed_var.get() * X_US_PER_DEG))}",
                  log=False)
        self.send(f"RATEY {max(5, int(self.speed_var.get() * Y_US_PER_DEG))}",
                  log=False)
        self.root.after(1500, self._check_pong)
        self.log_line("Nota: el UNO se resetea al conectar y su posicion "
                      "estimada vuelve a 0°/0°. Si la montura NO esta centrada "
                      "fisicamente, el primer movimiento puede dar un salto.",
                      "warn")

    def _check_pong(self):
        if not self.link.connected:
            return
        if self.pong_ok:
            self.conn_lbl.configure(text=f"conectado · {self.port_var.get()}",
                                    fg=C_OK)
            self.log_line(f"conectado; velocidad {self.speed_var.get():.1f} °/s"
                          f", watchdog {WDT_MS} ms", "info")
        else:
            self.conn_lbl.configure(text="sin respuesta del firmware",
                                    fg=C_WARN)
            self.log_line("no llego PONG: ¿es el puerto correcto? "
                          "(¿firmware ptztest cargado?)", "err")

    def disconnect(self, reason):
        if self._handshake_job:
            self.root.after_cancel(self._handshake_job)
            self._handshake_job = None
        self._pending_y = None
        self.link.close()
        self.conn_btn.configure(text="Conectar")
        self.conn_lbl.configure(text="desconectado", fg=C_MUTED)
        self.att = {"X": False, "Y": False}
        self.jogging = {"X": 0, "Y": 0}
        self.jog_src = {"X": set(), "Y": set()}
        self.log_line(reason, "warn")

    def send(self, cmd, log=True):
        if not self.link.connected:
            if log:
                self.log_line(f"no conectado, se ignora: {cmd}", "warn")
            return False
        try:
            self.link.write_line(cmd)
        except Exception as exc:
            self.disconnect(f"ERROR de escritura ({exc}); desconectado")
            return False
        self.last_tx = time.time()
        if cmd != "STATUS":
            self.awaiting_reply = True
            self.root.after(900, self._check_reply)
        if log:
            self.log_line(f"> {cmd}", "tx")
        return True

    def _check_reply(self):
        # Solo los acks OK/ERR/PONG limpian la bandera: las respuestas a los
        # polls STATUS no cuentan (si no, un comando perdido pasaria callado).
        if self.awaiting_reply:
            self.awaiting_reply = False
            self.log_line("sin respuesta del firmware a un comando", "err")

    # --------------------------- movimiento ---------------------------------
    def _axis_busy(self, axis):
        if self.jogging[axis]:
            return True
        if self.att[axis] and self.cur[axis] != self.tgt[axis]:
            return True
        return time.time() - self.move_sent[axis] < CMD_SETTLE_S

    def _mark_move(self, axis, tgt_us):
        # estado optimista: el gate nunca depende de un STATUS de hace 200 ms
        self.move_sent[axis] = time.time()
        self.att[axis] = True
        self.tgt[axis] = tgt_us

    def jog_start(self, axis, direction, src):
        self.jog_src[axis].add(src)
        if self.jogging[axis] == direction:
            return
        other = "Y" if axis == "X" else "X"
        if self.seq_var.get() and self._axis_busy(other):
            self.jog_src[axis].discard(src)
            self.log_line("bloqueado: el otro eje aun se mueve "
                          "(modo un-eje-a-la-vez)", "warn")
            return
        self._pending_y = None
        inv = INVERT_X if axis == "X" else INVERT_Y
        d = -direction if inv else direction
        lo, hi = (X_MIN, X_MAX) if axis == "X" else (Y_MIN, Y_MAX)
        tgt = hi if d > 0 else lo
        self.jogging[axis] = direction
        if self.send(f"{axis} {tgt}"):
            self._mark_move(axis, tgt)
        else:
            self.jogging[axis] = 0
            self.jog_src[axis].discard(src)

    def jog_stop(self, axis, src):
        self.jog_src[axis].discard(src)
        if self.jog_src[axis] or self.jogging[axis] == 0:
            return
        self.jogging[axis] = 0
        self.send("SX" if axis == "X" else "SY")
        self.tgt[axis] = self.cur[axis]

    def stop_all(self):
        self._pending_y = None
        self.jogging = {"X": 0, "Y": 0}
        self.jog_src = {"X": set(), "Y": set()}
        self.send("STOP")
        self.tgt = dict(self.cur)

    def set_speed(self, value):
        self.speed_var.set(value)
        self._push_speed()

    def _speed_changed(self, _evt=None):
        self.speed_lbl.configure(text=f"{self.speed_var.get():.1f} °/s")
        if self._speed_job:
            self.root.after_cancel(self._speed_job)
        self._speed_job = self.root.after(350, self._push_speed)

    def _push_speed(self):
        self._speed_job = None
        v = self.speed_var.get()
        self.speed_lbl.configure(text=f"{v:.1f} °/s")
        if self.link.connected:
            self.send(f"RATEX {max(5, int(v * X_US_PER_DEG))}", log=False)
            self.send(f"RATEY {max(5, int(v * Y_US_PER_DEG))}", log=False)
            self.log_line(f"velocidad -> {v:.1f} °/s", "info")

    def _goto_pair(self, x_us, y_us):
        self._pending_y = None
        if self.seq_var.get():
            if self._axis_busy("X") or self._axis_busy("Y"):
                self.log_line("espera: hay un movimiento en curso "
                              "(modo un-eje-a-la-vez)", "warn")
                return
            if self.send(f"X {x_us}"):
                self._mark_move("X", x_us)
                # el envio de Y lo dispara _apply_status cuando X termina
                self._pending_y = (y_us, time.time())
        else:
            if self.send(f"X {x_us}"):
                self._mark_move("X", x_us)
            if self.send(f"Y {y_us}"):
                self._mark_move("Y", y_us)

    def goto(self):
        try:
            gx = float(self.goto_x.get())
            gy = float(self.goto_y.get())
        except ValueError:
            self.log_line("valor de grados no valido", "err")
            return
        gx = max(-X_DEG_TOTAL / 2, min(X_DEG_TOTAL / 2, gx))
        gy = max(-Y_DEG_TOTAL / 2, min(Y_DEG_TOTAL / 2, gy))
        self.log_line(f"ir a X={gx:+.1f}° Y={gy:+.1f}°", "info")
        self._goto_pair(deg_to_us(gx, "X"), deg_to_us(gy, "Y"))

    def center(self):
        self.log_line("centrando", "info")
        self._goto_pair((X_MIN + X_MAX) // 2, (Y_MIN + Y_MAX) // 2)

    # ---------------------------- teclado -----------------------------------
    KEYMAP = {"Left": ("X", -1), "Right": ("X", +1),
              "Up": ("Y", +1), "Down": ("Y", -1)}

    def _key_press(self, event):
        if isinstance(event.widget, (tk.Spinbox, tk.Entry, ttk.Combobox)):
            return
        if event.keysym in self.KEYMAP and event.keysym not in self.keys_down:
            self.keys_down.add(event.keysym)
            axis, d = self.KEYMAP[event.keysym]
            self.jog_start(axis, d, "key")
        elif event.keysym == "space":
            self.stop_all()

    def _key_release(self, event):
        if event.keysym in self.KEYMAP:
            self.keys_down.discard(event.keysym)
            axis, _ = self.KEYMAP[event.keysym]
            self.jog_stop(axis, "key")

    def _app_focus_check(self):
        # Si la app entera perdio el foco con una tecla mantenida, Windows
        # entrega el KeyRelease a otra ventana: detener el jog huerfano.
        try:
            if self.root.focus_get() is not None:
                return
        except (KeyError, tk.TclError):
            return
        self.keys_down.clear()
        if self.jogging["X"] or self.jogging["Y"]:
            self.log_line("foco perdido con jog activo -> STOP", "warn")
            self.stop_all()

    # ------------------------- refresco periodico ---------------------------
    def _tick(self):
        try:
            while True:
                kind, line = self.rxq.get_nowait()
                if kind == "lost":
                    self.disconnect("conexion serie perdida")
                    continue
                if line.startswith(("OK", "ERR", "PONG")):
                    self.awaiting_reply = False
                m = STATUS_RE.match(line)
                if m:
                    self._apply_status(m)
                    if self.dbg_var.get():
                        self.log_line(line, "dim")
                elif line.startswith("PONG"):
                    self.pong_ok = True
                    self.log_line(f"< {line}", "rx")
                elif line.startswith("ERR"):
                    self.log_line(f"< {line}", "err")
                elif line.startswith("AUTO DETACH"):
                    self.log_line(f"< {line} (reposo, sin zumbido)", "warn")
                elif line.startswith("WATCHDOG"):
                    self.log_line(f"< {line} (enlace callado con movimiento)",
                                  "warn")
                elif line.startswith("UFOMOUNT"):
                    self.log_line(f"< {line}", "info")
                else:
                    self.log_line(f"< {line}", "rx")
        except queue.Empty:
            pass

        if self.link.connected and time.time() - self.last_tx > STATUS_POLL_S:
            try:
                self.link.write_line("STATUS")
                self.last_tx = time.time()
            except Exception as exc:
                self.disconnect(f"ERROR de escritura ({exc}); desconectado")

        self.root.after(50, self._tick)

    def _apply_status(self, m):
        self.att["X"], self.att["Y"] = m.group(1) == "1", m.group(2) == "1"
        self.cur["X"], tgt_x = int(m.group(3)), int(m.group(4))
        self.cur["Y"], tgt_y = int(m.group(7)), int(m.group(8))
        # el tgt reportado solo pisa el local pasada la ventana de asentamiento
        if time.time() - self.move_sent["X"] >= CMD_SETTLE_S:
            self.tgt["X"] = tgt_x
        if time.time() - self.move_sent["Y"] >= CMD_SETTLE_S:
            self.tgt["Y"] = tgt_y

        # goto secuencial dirigido por estado: Y sale cuando X termino
        if self._pending_y is not None:
            y_us, t0 = self._pending_y
            if (time.time() - t0 >= CMD_SETTLE_S and self.jogging["X"] == 0
                    and self.cur["X"] == self.tgt["X"]):
                self._pending_y = None
                if self.send(f"Y {y_us}"):
                    self._mark_move("Y", y_us)

        dx = us_to_deg(self.cur["X"], "X")
        dy = us_to_deg(self.cur["Y"], "Y")
        self.deg_x.configure(text=f"{dx:+.1f}°")
        self.deg_y.configure(text=f"{dy:+.1f}°")
        sx = "moviendo" if self.cur["X"] != self.tgt["X"] else \
             ("sosteniendo" if self.att["X"] else "reposo")
        sy = "moviendo" if self.cur["Y"] != self.tgt["Y"] else \
             ("sosteniendo" if self.att["Y"] else "reposo")
        self.us_x.configure(text=f"{self.cur['X']} us · {sx}")
        self.us_y.configure(text=f"{self.cur['Y']} us · {sy}")
        self._redraw_dials()

    # ------------------- diales: donde mira la camara -----------------------
    FOV_HALF = 22          # semiancho del cono de vision dibujado (grados)

    def _redraw_dials(self):
        self._draw_pan_dial()
        self._draw_tilt_dial()

    def _draw_pan_dial(self):
        """Vista superior: arco de 270 con la zona ciega trasera rayada.
        0 grados = frente (arriba); + = giro a la derecha en pantalla."""
        c = self.dial_x
        w, h = c.winfo_width(), c.winfo_height()
        if w < 60 or h < 60:
            return
        c.delete("all")
        cx, cy = w / 2, h / 2
        rad = min(w, h) / 2 - 16
        box = (cx - rad, cy - rad, cx + rad, cy + rad)

        # disco alcanzable (270) + sector ciego trasero (90, rayado)
        c.create_arc(*box, start=-45, extent=270, style="pieslice",
                     fill=C_PANEL2, outline=C_LINE)
        c.create_arc(*box, start=225, extent=90, style="pieslice",
                     fill=C_BG, outline=C_LINE, stipple="gray25")

        pan = us_to_deg(self.cur["X"], "X")
        tgt = us_to_deg(self.tgt["X"], "X")

        # cono de vision de la camara
        c.create_arc(*box, start=90 - pan - self.FOV_HALF,
                     extent=2 * self.FOV_HALF, style="pieslice",
                     fill="#4A2E14", outline="")

        # ticks y etiquetas
        for deg in range(-135, 136, 45):
            a = math.radians(90 - deg)
            c.create_line(cx + (rad - 6) * math.cos(a),
                          cy - (rad - 6) * math.sin(a),
                          cx + rad * math.cos(a),
                          cy - rad * math.sin(a), fill=C_MUTED)
        for deg, txt in ((-135, "-135°"), (0, "0°"), (135, "+135°")):
            a = math.radians(90 - deg)
            c.create_text(cx + (rad + 11) * math.cos(a),
                          cy - (rad + 11) * math.sin(a),
                          text=txt, fill=C_MUTED, font=("Segoe UI", 7))

        # objetivo (punteado) y aguja de la camara
        if self.tgt["X"] != self.cur["X"]:
            a = math.radians(90 - tgt)
            c.create_line(cx, cy, cx + (rad - 4) * math.cos(a),
                          cy - (rad - 4) * math.sin(a),
                          fill=C_MUTED, dash=(3, 3))
        a = math.radians(90 - pan)
        c.create_line(cx, cy, cx + (rad - 4) * math.cos(a),
                      cy - (rad - 4) * math.sin(a),
                      fill=C_X, width=3, arrow=tk.LAST)
        c.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, fill=C_TEXT, outline="")

    def _draw_tilt_dial(self):
        """Vista lateral: semicirculo de 180. 0 = horizonte (derecha),
        +90 = cenit (arriba), -90 = nadir (abajo)."""
        c = self.dial_y
        w, h = c.winfo_width(), c.winfo_height()
        if w < 60 or h < 60:
            return
        c.delete("all")
        rad = min(h / 2 - 14, w - 74)
        cx, cy = max(10, (w - rad) / 2 - 14), h / 2
        box = (cx - rad, cy - rad, cx + rad, cy + rad)

        c.create_arc(*box, start=-90, extent=180, style="pieslice",
                     fill=C_PANEL2, outline=C_LINE)

        tilt = us_to_deg(self.cur["Y"], "Y")
        tgt = us_to_deg(self.tgt["Y"], "Y")

        # cono de vision
        c.create_arc(*box, start=tilt - self.FOV_HALF,
                     extent=2 * self.FOV_HALF, style="pieslice",
                     fill="#173246", outline="")

        # horizonte
        c.create_line(cx, cy, cx + rad, cy, fill=C_LINE, dash=(3, 3))

        # ticks y etiquetas
        for deg in range(-90, 91, 45):
            a = math.radians(deg)
            c.create_line(cx + (rad - 6) * math.cos(a),
                          cy - (rad - 6) * math.sin(a),
                          cx + rad * math.cos(a),
                          cy - rad * math.sin(a), fill=C_MUTED)
        for deg, txt in ((90, "+90° cielo"), (0, "0° horizonte"),
                         (-90, "-90° suelo")):
            a = math.radians(deg)
            c.create_text(cx + (rad + 8) * math.cos(a) + (18 if deg == 0 else 0),
                          cy - (rad + 10) * math.sin(a),
                          text=txt, fill=C_MUTED, font=("Segoe UI", 7),
                          anchor="w" if deg == 0 else "center")

        # objetivo (punteado) y aguja de la camara
        if self.tgt["Y"] != self.cur["Y"]:
            a = math.radians(tgt)
            c.create_line(cx, cy, cx + (rad - 4) * math.cos(a),
                          cy - (rad - 4) * math.sin(a),
                          fill=C_MUTED, dash=(3, 3))
        a = math.radians(tilt)
        c.create_line(cx, cy, cx + (rad - 4) * math.cos(a),
                      cy - (rad - 4) * math.sin(a),
                      fill=C_Y, width=3, arrow=tk.LAST)
        c.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, fill=C_TEXT, outline="")

    def _on_close(self):
        self._pending_y = None
        if self.link.connected:
            try:
                self.link.write_line("STOP")
            except Exception:
                pass
        self.link.close()
        self.root.destroy()


def check_mode(port):
    print(f"Abriendo {port} (el UNO se resetea)...")
    ser = serial.Serial(port, BAUD, timeout=0.5)
    time.sleep(BOOT_WAIT_S)
    banner = ser.read_all().decode("ascii", errors="replace").strip()
    print(f"BOOT: {banner}")
    ser.write(b"PING\n")
    time.sleep(0.4)
    print(f"RESP: {ser.read_all().decode('ascii', errors='replace').strip()}")
    ser.write(b"STATUS\n")
    time.sleep(0.4)
    print(f"STATUS: {ser.read_all().decode('ascii', errors='replace').strip()}")
    ser.close()
    print("OK")


def main():
    ap = argparse.ArgumentParser(description="Control PTZ ufomount")
    ap.add_argument("--check", action="store_true",
                    help="prueba de conexion sin GUI")
    ap.add_argument("--port", default=PORT_DEFAULT)
    args = ap.parse_args()
    if args.check:
        check_mode(args.port)
        return
    root = tk.Tk()
    PtzGui(root, port=args.port)
    root.mainloop()


if __name__ == "__main__":
    main()
