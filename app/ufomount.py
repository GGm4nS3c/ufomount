#!/usr/bin/env python3
"""UFOMOUNT: menu de inicio y modos de uso de la montura con la Raspberry.

Modos:
    Montura            solo el control PTZ: velocidades, posicion de los ejes
                       X e Y, ir a, limites del tilt y mando. (Aqui iran mapas,
                       calibracion al norte, etc.)
    Camara             solo la camara: vista en vivo y todos sus ajustes.
    Montura completa   camara y montura en una ventana, con los paneles a gusto
                       (la division se arrastra). Doble clic en la imagen =
                       pantalla completa; ahi la montura se sigue moviendo con
                       las flechas, el mando o los botones sobre la imagen.
                       Esc o doble clic para salir.

No modifica las interfaces existentes: usa scripts/ptz_gui.py y
asicam/pc/asicam_gui.py tal cual, cada una dentro de un contenedor de esta
ventana. La montura se maneja por la Raspberry (servidor asicam, PTZ en el
puerto 5000); el Arduino queda como respaldo con UfomountPTZ.exe.

Uso:
    python app/ufomount.py                       # menu de inicio
    python app/ufomount.py --mode full           # directo a un modo: ptz, cam, full
    python app/ufomount.py --mode full --fullscreen
    python app/ufomount.py --host 192.168.200.159
    python app/ufomount.py --simulate            # con un servidor simulado local
    python app/ufomount.py --simulate --selftest # prueba automatica y sale
"""

import argparse
import json
import os
import socket
import sys
import threading
import time
import tkinter as tk
import traceback
from pathlib import Path
from tkinter import ttk

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
for _sub in (("app",), ("scripts",), ("asicam", "pc")):
    _p = os.path.join(REPO, *_sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import asicam_gui as CAM  # noqa: E402
import direct_link  # noqa: E402
import ptz_gui as PTZ  # noqa: E402
from asicam_client import discover  # noqa: E402

APP = "UFOMOUNT"
VERSION = "0.2.0"
CONFIG_PATH = Path.home() / ".ufomount_app.json"
PTZ_PORT = 5000
CAM_CTRL_PORT = 5300
HUD = "hud"   # etiqueta de lo que se dibuja sobre la imagen en pantalla completa

C_BG, C_PANEL, C_PANEL2 = CAM.C_BG, CAM.C_PANEL, CAM.C_PANEL2
C_TEXT, C_MUTED, C_LINE, C_ACC = CAM.C_TEXT, CAM.C_MUTED, CAM.C_LINE, CAM.C_ACC
C_OK, C_WARN, C_ERR = CAM.C_OK, CAM.C_WARN, CAM.C_ERR
C_X, C_Y = PTZ.C_X, PTZ.C_Y
C_HUD = "#0B0F13"
C_STOP = "#5A2A2A"

MODES = [
    ("ptz", "Montura",
     "Solo la montura (PTZ): velocidades, posición de los ejes X e Y, ir a, límites del tilt y mando."),
    ("cam", "Cámara",
     "Solo la cámara: vista en vivo, histograma, exposición, ganancia, formato, foto y video."),
    ("full", "Montura completa",
     "Cámara y montura juntas, con los paneles a gusto. Doble clic en la imagen: pantalla completa, "
     "y la montura se sigue moviendo."),
]
MODE_NAMES = {key: name for key, name, _ in MODES}

SAVE_PREFS = True


def load_cfg():
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def save_cfg(**values):
    if not SAVE_PREFS:
        return
    cfg = load_cfg()
    cfg.update(values)
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass


def no_save_prefs():
    """--simulate / --selftest: no tocar las preferencias guardadas del usuario."""
    global SAVE_PREFS
    SAVE_PREFS = False
    CAM.save_cfg = lambda cfg: None
    PTZ.save_cfg = lambda **values: None


def last_host():
    return CAM.load_cfg().get("host") or ""


def remember_host(host):
    """La Raspberry quedo en `host`: los tres modos parten de ahi."""
    if not SAVE_PREFS:
        return
    cfg = CAM.load_cfg()
    hosts = [h for h in cfg.get("hosts", []) if isinstance(h, str) and h != host]
    cfg["host"], cfg["hosts"] = host, ([host] + hosts)[:8]
    CAM.save_cfg(cfg)
    PTZ.save_cfg(port="%s:%d" % (host, PTZ_PORT))


def known_hints():
    """IPs donde ya se encontro o uso la Pi: Buscar recorre sus redes /24, asi la encuentra
    aunque este en otra subred (el broadcast no cruza routers) o haya cambiado de IP."""
    hints = [h for h in CAM.load_cfg().get("hosts", []) if isinstance(h, str) and h]
    hints += PTZ.ip_hints(PTZ.load_cfg().get("port", ""))
    return list(dict.fromkeys(hints))


def nets_of(hints):
    return sorted({h.split(":")[0].rsplit(".", 1)[0] + ".x" for h in PTZ.ip_hints(" ".join(hints))})


NOT_FOUND_HINT = ("no encontré la Raspberry. Si está en otra red, escribe una IP de esa red "
                  "(p. ej. 192.168.201.0) y pulsa Buscar")


def default_ptz_port(host=None):
    if host:
        return "%s:%d" % (host, PTZ_PORT)
    saved = PTZ.load_cfg().get("port", "")
    if PTZ.is_net_port(saved):
        return saved
    h = last_host()
    return "%s:%d" % (h, PTZ_PORT) if h else ""


def cancel_afters(root):
    """Cancela los temporizadores pendientes de una ventana que se va a cerrar.

    Las interfaces incrustadas se refrescan con after(); si quedan pendientes,
    Tcl los dispara igual cuando corre la ventana siguiente (mismo hilo) y
    avisa "invalid command name".
    """
    try:
        for job in root.tk.splitlist(root.tk.call("after", "info")):
            # solo cancelar: el comando Python lo borra el widget al destruirse
            # (after_cancel() lo borraria antes y destroy() fallaria)
            root.tk.call("after", "cancel", job)
    except tk.TclError:
        pass


def style_root(root):
    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure("TButton", background=C_PANEL2, foreground=C_TEXT, borderwidth=0,
                 focuscolor=C_PANEL2, padding=5)
    st.map("TButton", background=[("active", C_LINE), ("disabled", C_PANEL)],
           foreground=[("disabled", C_MUTED)])
    st.configure("TCombobox", fieldbackground=C_PANEL2, background=C_PANEL2, foreground=C_TEXT,
                 arrowcolor=C_TEXT, bordercolor=C_LINE, lightcolor=C_PANEL2, darkcolor=C_PANEL2,
                 selectbackground=C_PANEL2, selectforeground=C_TEXT)
    root.option_add("*TCombobox*Listbox.background", C_PANEL2)
    root.option_add("*TCombobox*Listbox.foreground", C_TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", C_LINE)


# ----------------------------------------------------------------------------
# Conexion directa: sin router ni DHCP, el programa entrega las IP
# ----------------------------------------------------------------------------

DIRECT = direct_link.DirectLink()
DIRECT_TEXT = ("Para conectar la montura directo a este PC (o por un switch) cuando no hay un router que "
               "entregue IP. El programa le pone una IP a este puerto y le da otra a la Raspberry. "
               "No hay que cambiar nada en ella.\n\n"
               "Windows pedirá permiso de administrador. Al detener, o al cerrar UFOMOUNT, "
               "el puerto queda como estaba.")


class DirectDialog:
    """Ventana de la conexion directa; `on_found(ip)` al contestar la Raspberry."""

    open_one = None

    def __init__(self, root, on_found=None, link=None):
        self.link = link or DIRECT
        self.on_found = on_found
        self.adapters = []
        self._scan = {}              # respuesta del hilo que mira los puertos
        self._probe = {}             # respuesta del hilo que busca la Raspberry
        self._shown = None
        self._announced = None
        self._ready_t = None
        t = self.win = tk.Toplevel(root, bg=C_BG)
        t.title("Conexión directa (sin router)")
        t.geometry("600x430+%d+%d" % (max(0, root.winfo_rootx() + (root.winfo_width() - 600) // 2),
                                      max(0, root.winfo_rooty() + (root.winfo_height() - 430) // 3)))
        t.minsize(520, 380)
        t.transient(root)
        st = ttk.Style(root)         # solo aqui: la lista de solo lectura sale clara con clam
        st.map("Direct.TCombobox", fieldbackground=[("disabled", C_PANEL), ("readonly", C_PANEL2)],
               foreground=[("disabled", C_MUTED), ("readonly", C_TEXT)],
               selectbackground=[("readonly", C_PANEL2)], selectforeground=[("readonly", C_TEXT)])
        tk.Label(t, text="Conexión directa (sin router)", bg=C_BG, fg=C_TEXT,
                 font=("Segoe UI", 14, "bold")).pack(anchor="w", padx=18, pady=(16, 4))
        tk.Label(t, text=DIRECT_TEXT, bg=C_BG, fg=C_MUTED, justify="left", wraplength=560).pack(
            anchor="w", padx=18)
        row = tk.Frame(t, bg=C_BG)
        row.pack(fill="x", padx=18, pady=(12, 4))
        tk.Label(row, text="Puerto Ethernet:", bg=C_BG, fg=C_MUTED).pack(side="left")
        self.adapter_var = tk.StringVar()
        self.adapter_cb = ttk.Combobox(row, textvariable=self.adapter_var, state="readonly", width=44,
                                       style="Direct.TCombobox")
        self.adapter_cb.pack(side="left", padx=6, fill="x", expand=True)
        self.rescan_btn = ttk.Button(row, text="↻", width=3, command=self.scan)
        self.rescan_btn.pack(side="left")
        # la barra antes que el registro: asi no queda fuera si la ventana es chica
        bar = tk.Frame(t, bg=C_BG)
        bar.pack(fill="x", side="bottom", padx=18, pady=(2, 14))
        self.go_btn = ttk.Button(bar, text="Activar", command=self.toggle)
        self.go_btn.pack(side="left")
        ttk.Button(bar, text="Cerrar", command=self.close).pack(side="right")
        self.note = tk.Label(bar, text="", bg=C_BG, fg=C_MUTED, justify="left", wraplength=380)
        self.note.pack(side="left", padx=10)
        self.log = tk.Text(t, height=6, bg=C_PANEL, fg=C_TEXT, bd=0, highlightthickness=0, wrap="word",
                           padx=8, pady=6, state="disabled", takefocus=0)
        self.log.pack(fill="both", expand=True, padx=18, pady=6)
        t.protocol("WM_DELETE_WINDOW", self.close)
        t.bind("<Escape>", self._escape)
        DirectDialog.open_one = self
        self.scan()
        self._tick()

    @classmethod
    def show(cls, root, on_found=None):
        d = cls.open_one
        if d is not None and d.alive():
            d.win.lift()
            return d
        return cls(root, on_found)

    def alive(self):
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def _escape(self, _e):
        self.close()
        return "break"

    def close(self):
        """Solo cierra la ventana: la conexion directa sigue hasta Detener o salir."""
        DirectDialog.open_one = None
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    def scan(self):
        if self.link.active:
            return
        self.rescan_btn.configure(state="disabled")
        self.adapter_var.set("buscando puertos…")
        box = self._scan = {}

        def work():
            try:
                box["res"] = [a for a in direct_link.all_adapters() if direct_link.is_wired(a)]
            except Exception as e:  # noqa: BLE001
                box["res"] = e
        threading.Thread(target=work, daemon=True).start()

    def _scanned(self, res):
        self.rescan_btn.configure(state="normal")
        if isinstance(res, Exception):
            self.adapter_var.set("no pude leer los puertos: %s" % res)
            return
        self.adapters = sorted(res, key=lambda a: bool(direct_link.problem(a)))
        names = []
        for a in self.adapters:
            names.append("%s — %s" % (a["name"], direct_link.problem(a) or "listo"))
        self.adapter_cb.configure(values=names)
        if names:
            self.adapter_cb.current(0)
        else:
            self.adapter_var.set("este PC no tiene un puerto Ethernet")

    def selected(self):
        i = self.adapter_cb.current()
        return self.adapters[i] if 0 <= i < len(self.adapters) else None

    def toggle(self):
        if self.link.active:
            self.link.stop()
            return
        a = self.selected()
        if a is None:
            return
        why = direct_link.problem(a)
        if why:
            self.note.configure(text="%s: %s" % (a["name"], why), fg=C_WARN)
            return
        self.note.configure(text="", fg=C_MUTED)
        self._announced = self._ready_t = None
        self._probe = {}
        self.link.start(a["index"])

    def _tick(self):
        if not self.alive():
            return
        if "res" in self._scan:
            res, self._scan = self._scan["res"], {}
            self._scanned(res)
        if self.link.state == "ready":
            self._look_for_mount()
        self.go_btn.configure(text="Detener" if self.link.active else "Activar")
        self.adapter_cb.configure(state="disabled" if self.link.active else "readonly")
        lines = "\n".join(self.link.lines)
        if lines != self._shown:
            self._shown = lines
            self.log.configure(state="normal")
            self.log.delete("1.0", "end")
            self.log.insert("end", lines)
            self.log.see("end")
            self.log.configure(state="disabled")
        self.win.after(300, self._tick)

    def _look_for_mount(self):
        """Cuando la montura ya tiene IP y su servidor contesta, avisa una vez."""
        lk = self.link
        if self._ready_t is None:
            self._ready_t = time.monotonic()
        if self._announced:
            return
        if "ip" in self._probe:
            ip, self._probe = self._probe["ip"], {}
            if ip:
                self._announced = ip
                remember_host(ip)
                lk._line("la Raspberry contesta en %s: lista para conectar" % ip)
                self.note.configure(text="Raspberry en %s" % ip, fg=C_OK)
                if self.on_found is not None:
                    self.on_found(ip)
        elif not self._probe and lk.mounts:
            box = self._probe = {"busy": True}

            def work():
                ip = lk.reachable()
                if not ip:
                    time.sleep(1.0)
                box["ip"] = ip
            threading.Thread(target=work, daemon=True).start()
        elif not lk.mounts and time.monotonic() - self._ready_t > 45:
            self.note.configure(text="si no aparece, desconecta y conecta el cable de la montura", fg=C_WARN)


# ----------------------------------------------------------------------------
# Contenedor para las interfaces existentes
# ----------------------------------------------------------------------------

class Embed(tk.Frame):
    """Hace de ventana para una interfaz escrita para Tk().

    Las interfaces existentes llaman a title(), geometry(), protocol()... de
    su "root" y le ponen el teclado. Aqui esas llamadas no hacen nada y el
    teclado se engancha a la ventana real, sumandose sin pisar otros enlaces.
    """

    KEY_EVENTS = ("<KeyPress>", "<KeyRelease>", "<FocusOut>")

    def title(self, *args):
        return ""

    def geometry(self, *args):
        return ""

    def minsize(self, *args):
        return None

    def protocol(self, *args):
        return ""

    def bind(self, sequence=None, func=None, add=None):
        if sequence in self.KEY_EVENTS and func is not None:
            return self.winfo_toplevel().bind(sequence, func, "+")
        return super().bind(sequence, func, add)

    def top_bar(self):
        """Barra superior de la interfaz incrustada (fila 0 de su grilla), o None."""
        row0 = self.grid_slaves(row=0)
        return row0[0] if row0 else None


class PiPtz(PTZ.PtzGui):
    """La interfaz PTZ de siempre, solo por red: la montura es la Raspberry."""

    def _ports(self):
        ports = []
        saved = PTZ.load_cfg().get("port", "")
        if PTZ.is_net_port(saved):
            ports.append(saved)
        host = last_host()
        if host and "%s:%d" % (host, PTZ_PORT) not in ports:
            ports.append("%s:%d" % (host, PTZ_PORT))
        return ports

    def _key_press(self, event):
        # las flechas sobre un deslizador o el log no mueven la montura
        if isinstance(event.widget, (ttk.Scale, tk.Scale, tk.Text, tk.Listbox)):
            return
        super()._key_press(event)

    def find_mounts(self):
        """Como la original, pero recorre tambien las redes donde ya se uso la Pi."""
        hints = set(PTZ.ip_hints(self.port_var.get()))
        hints.update(PTZ.ip_hints(PTZ.load_cfg().get("port", "")))
        hints.update(known_hints())
        nets = nets_of(sorted(hints))
        self.log_line("buscando la Raspberry en la red local" + (" y en %s" % ", ".join(nets) if nets else "")
                      + "...", "info")
        self.find_btn.configure(state="disabled")
        threading.Thread(target=lambda: self.rxq.put(("found", PTZ.discover(hints))), daemon=True).start()

    def _show_found(self, found):
        super()._show_found(found)        # si no hay nada, ya explica como buscar en otra red
        if found and not self.link.connected:
            self.toggle_conn()            # encontrada: conectar sin otro clic


def draw_icon(c, key):
    """Dibujito de cada modo para el menu (lienzo de 64x64)."""
    def dial(cx, cy, r):
        c.create_arc(cx - r, cy - r, cx + r, cy + r, start=-45, extent=270, style="arc",
                     outline=C_MUTED, width=2)
        c.create_line(cx, cy, cx + r * 0.55, cy - r * 0.75, fill=C_X, width=3, arrow="last")
        c.create_line(cx, cy, cx + r * 0.9, cy - r * 0.1, fill=C_Y, width=3, arrow="last")
        c.create_oval(cx - 3, cy - 3, cx + 3, cy + 3, fill=C_TEXT, outline="")

    def camera(x, y, s):
        c.create_rectangle(x, y + s * 0.18, x + s, y + s * 0.82, outline=C_ACC, width=2)
        c.create_rectangle(x + s * 0.15, y + s * 0.05, x + s * 0.4, y + s * 0.18, outline=C_ACC, width=2)
        c.create_oval(x + s * 0.3, y + s * 0.3, x + s * 0.7, y + s * 0.7, outline=C_ACC, width=2)

    if key == "ptz":
        dial(32, 34, 26)
    elif key == "cam":
        camera(8, 10, 48)
    else:
        dial(24, 38, 20)
        camera(36, 4, 26)


# ----------------------------------------------------------------------------
# Menu de inicio
# ----------------------------------------------------------------------------

class Menu:
    def __init__(self, last=None, test=None):
        self.choice = None
        r = self.root = tk.Tk()
        r.title(APP)
        r.configure(bg=C_BG)
        r.geometry("760x560")
        r.minsize(640, 520)
        style_root(r)
        r.protocol("WM_DELETE_WINDOW", r.destroy)

        head = tk.Frame(r, bg=C_BG)
        head.pack(fill="x", padx=28, pady=(26, 8))
        tk.Label(head, text=APP, bg=C_BG, fg=C_TEXT, font=("Segoe UI", 24, "bold")).pack(anchor="w")
        tk.Label(head, text="¿Cómo quieres usar la montura?", bg=C_BG, fg=C_MUTED,
                 font=("Segoe UI", 11)).pack(anchor="w")

        self.cards = {}
        for i, (key, name, desc) in enumerate(MODES):
            self.cards[key] = self._card(r, key, name, desc, str(i + 1), key == last)
            r.bind(str(i + 1), lambda e, k=key: self.choose(k))
            r.bind("<KP_%d>" % (i + 1), lambda e, k=key: self.choose(k))

        foot = tk.Frame(r, bg=C_BG)
        foot.pack(fill="x", side="bottom", padx=28, pady=(0, 16))
        host = last_host()
        self.host_lbl = tk.Label(foot, text=("Raspberry: %s" % host) if host else "Raspberry: se busca al conectar",
                                 bg=C_BG, fg=C_MUTED)
        self.host_lbl.pack(side="left")
        self.direct_btn = ttk.Button(foot, text="Conexión directa (sin router)…", command=self.direct)
        self.direct_btn.pack(side="left", padx=12)
        tk.Label(foot, text="v%s  ·  teclas 1, 2, 3  ·  Esc: salir" % VERSION, bg=C_BG,
                 fg=C_MUTED).pack(side="right")
        r.bind("<Escape>", lambda e: r.destroy())
        if test is not None:
            test.attach(self)

    def _card(self, parent, key, name, desc, num, last):
        edge = C_ACC if last else C_LINE
        f = tk.Frame(parent, bg=C_PANEL, highlightthickness=2, highlightbackground=edge,
                     highlightcolor=edge, cursor="hand2")
        f.pack(fill="x", padx=28, pady=7)
        icon = tk.Canvas(f, width=64, height=64, bg=C_PANEL, highlightthickness=0)
        icon.pack(side="left", padx=(14, 12), pady=12)
        draw_icon(icon, key)
        tk.Label(f, text=num, bg=C_PANEL2, fg=C_TEXT, font=("Segoe UI", 11, "bold"),
                 width=3, pady=4).pack(side="right", padx=16)
        txt = tk.Frame(f, bg=C_PANEL)
        txt.pack(side="left", fill="x", expand=True, pady=10)
        tk.Label(txt, text=name, bg=C_PANEL, fg=C_TEXT, font=("Segoe UI", 14, "bold"),
                 anchor="w").pack(fill="x")
        tk.Label(txt, text=desc, bg=C_PANEL, fg=C_MUTED, font=("Segoe UI", 9), anchor="w",
                 justify="left", wraplength=500).pack(fill="x")

        def wire(w):
            w.bind("<Button-1>", lambda e: self.choose(key))
            w.bind("<Enter>", lambda e: f.configure(highlightbackground=C_ACC))
            w.bind("<Leave>", lambda e: f.configure(highlightbackground=edge))
            for child in w.winfo_children():
                wire(child)
        wire(f)
        return f

    def direct(self):
        DirectDialog.show(self.root, lambda ip: self.host_lbl.configure(text="Raspberry: %s" % ip))

    def choose(self, key):
        self.choice = key
        save_cfg(last_mode=key)
        cancel_afters(self.root)
        self.root.destroy()


# ----------------------------------------------------------------------------
# Ventana de cada modo
# ----------------------------------------------------------------------------

class ModeWindow:
    def __init__(self, mode, host=None, test=None):
        self.mode = mode
        self.next = None             # "menu" si hay que volver al menu
        self.ptz = None              # PiPtz
        self.cam = None              # AsiCamGui
        self.fullscreen = False
        self._hud = {}
        self._btns = {}
        self._ptz_probing = False    # hay una prueba de conexion de la montura en curso
        r = self.root = tk.Tk()
        r.title("%s · %s" % (APP, MODE_NAMES[mode]))
        r.configure(bg=C_BG)
        style_root(r)
        r.protocol("WM_DELETE_WINDOW", self.close)
        {"ptz": self._build_ptz, "cam": self._build_cam, "full": self._build_full}[mode](host)
        # teclado: las flechas mueven la montura salvo mientras se escribe en un campo
        r.bind_all("<ButtonPress-1>", self._click_focus, "+")
        r.bind_all("<<ComboboxSelected>>", lambda e: self.root.after_idle(self.root.focus_set), "+")
        r.after(150, self._take_focus)
        if test is not None:
            test.attach(self)

    # campos donde se escribe: ahi las flechas mueven el cursor, no la montura
    TYPING = (tk.Entry, tk.Spinbox, ttk.Entry, ttk.Combobox, ttk.Spinbox)

    def _take_focus(self):
        """La ventana se crea despues de cerrar el menu y Windows no le da el teclado
        sola: sin esto las flechas no hacen nada hasta hacerle clic."""
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            pass

    def _click_focus(self, e):
        """Un clic fuera de un campo de texto devuelve el teclado a la ventana, para que
        las flechas vuelvan a mover la montura (si no, quedan en la lista o el campo)."""
        w = e.widget
        if isinstance(w, str) or isinstance(w, self.TYPING):
            return                      # p. ej. la lista desplegable de un combobox
        try:
            self.root.focus_set()
        except tk.TclError:
            pass

    # ---------------------------------------------------------------- modos
    def _menu_button(self, bar, column):
        ttk.Button(bar, text="◂ Menú", command=self.to_menu).grid(row=0, column=column, padx=(10, 2))

    def _build_ptz(self, host):
        r = self.root
        r.geometry("1000x840")
        r.minsize(780, 740)
        r.columnconfigure(0, weight=1)
        r.rowconfigure(0, weight=1)
        box = Embed(r, bg=C_BG)
        box.grid(row=0, column=0, sticky="nsew")
        self.ptz = PiPtz(box, port=default_ptz_port(host))
        self._menu_button(box.top_bar(), column=8)
        self._probe_ptz_then_connect()

    def _probe_ptz_then_connect(self, quiet=False):
        """Conecta la montura si la Raspberry responde (sin congelar la ventana si esta
        apagada: la conexion de la interfaz PTZ bloquea hasta 5 s).

        Se prueba por TCP el puerto de control de la camara (el servidor lo abre haya o
        no camara) y no molesta a otro cliente de la montura. La busqueda UDP queda de
        respaldo (ptz.py solo): la Pi puede contestarla desde su otra IP y el firewall
        de Windows la descarta para programas sin permiso propio (pythonw, el .exe).
        """
        port = self.ptz.port_var.get().strip()
        if not PTZ.is_net_port(port) or self._ptz_probing:
            return
        self._ptz_probing = True
        host = port.split(":")[0]
        result = {}

        def probe():
            try:
                socket.create_connection((host, CAM_CTRL_PORT), timeout=1.5).close()
                result["ok"] = True
                return
            except OSError:
                pass
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1.2)
            try:
                s.sendto(PTZ.DISCOVERY_MSG, (host, PTZ.DISCOVERY_PORT))
                result["ok"] = s.recvfrom(256)[0].startswith(b"UFOMOUNT")
            except OSError:
                result["ok"] = False
            finally:
                s.close()

        def check():
            if "ok" not in result:
                self.root.after(100, check)
                return
            self._ptz_probing = False
            if result["ok"]:
                if not self.ptz.link.connected:
                    self.ptz.toggle_conn()
            elif not quiet:
                self.ptz.log_line("la Raspberry no contesta en %s: revisa que esté encendida "
                                  "o pulsa Buscar" % port, "warn")
        threading.Thread(target=probe, daemon=True).start()
        self.root.after(100, check)

    def _build_cam(self, host):
        r = self.root
        r.geometry("1360x880")
        r.minsize(1000, 680)
        r.columnconfigure(0, weight=1)
        r.rowconfigure(0, weight=1)
        box = Embed(r, bg=C_BG)
        box.grid(row=0, column=0, sticky="nsew")
        self.cam = CAM.AsiCamGui(box, host=host)
        self._menu_button(box.top_bar(), column=7)

    def _build_full(self, host):
        r = self.root
        r.geometry("1680x940")
        try:
            r.state("zoomed")
        except tk.TclError:
            pass
        r.minsize(1200, 720)
        r.columnconfigure(0, weight=1)
        r.rowconfigure(1, weight=1)
        self.paned = tk.PanedWindow(r, orient="horizontal", sashwidth=6, bg=C_LINE, bd=0,
                                    sashrelief="flat")
        self.paned.grid(row=1, column=0, sticky="nsew")
        self.cam_box = Embed(self.paned, bg=C_BG)
        self.ptz_box = Embed(self.paned, bg=C_BG)
        # la montura primero: asi quedan los estilos de la camara (deslizadores visibles)
        self.ptz = PiPtz(self.ptz_box, port=default_ptz_port(host))
        self.cam = CAM.AsiCamGui(self.cam_box, host=host)
        self.paned.add(self.cam_box, minsize=700, stretch="always")
        # la division se arrastra; de entrada, ancho suficiente para los dos diales
        self.paned.add(self.ptz_box, minsize=520, width=680, stretch="never")
        for box in (self.cam_box, self.ptz_box):   # una sola barra de conexion para las dos
            bar = box.top_bar()
            if bar is not None:
                bar.grid_remove()
        self._build_header(host)

        c = self.cam.canvas
        c.bind("<Double-Button-1>", self._double_click)       # aqui: pantalla completa (no zoom)
        c.bind("<ButtonRelease-1>", self._hud_release, add="+")
        c.bind("<Configure>", lambda e: self._hud_place(), add="+")
        r.bind("<Escape>", lambda e: self.set_fullscreen(False))
        r.bind("<F11>", lambda e: self.set_fullscreen(not self.fullscreen))
        for seq, step in (("<KeyPress-plus>", 1), ("<KeyPress-KP_Add>", 1),
                          ("<KeyPress-minus>", -1), ("<KeyPress-KP_Subtract>", -1)):
            r.bind(seq, lambda e, s=step: self._speed_key(e, s))
        # la montura no depende de la camara: se conecta aunque la Pi no tenga camara, y
        # la camara se reintenta sola (asi conecta si se enchufa despues)
        self.want = bool(self.host_var.get().strip())
        self._ptz_try_t = 0.0
        self._ptz_tries = 0
        self._cam_try_t = time.monotonic()   # al abrir, la camara ya intenta por su cuenta
        self._header_tick()

    def _build_header(self, host):
        bar = tk.Frame(self.root, bg=C_PANEL, padx=8, pady=6)
        bar.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        self.header = bar
        tk.Label(bar, text=APP, bg=C_PANEL, fg=C_TEXT, font=("Segoe UI", 13, "bold")).pack(
            side="left", padx=(2, 6))
        tk.Label(bar, text="montura completa", bg=C_PANEL, fg=C_MUTED).pack(side="left", padx=(0, 16))
        tk.Label(bar, text="Raspberry:", bg=C_PANEL, fg=C_MUTED).pack(side="left")
        self.host_var = tk.StringVar(value=host or last_host())
        self.host_cb = ttk.Combobox(bar, textvariable=self.host_var, width=20,
                                    values=CAM.load_cfg().get("hosts", []))
        self.host_cb.pack(side="left", padx=4)
        self.find_btn = ttk.Button(bar, text="Buscar", command=self.find)
        self.find_btn.pack(side="left", padx=(4, 0))
        self.conn_btn = ttk.Button(bar, text="Conectar", command=self.toggle_connection)
        self.conn_btn.pack(side="left", padx=8)
        self.direct_btn = ttk.Button(bar, text="Sin router…", command=self.direct)
        self.direct_btn.pack(side="left")
        self.hint_lbl = tk.Label(bar, text="", bg=C_PANEL, fg=C_MUTED)
        self.hint_lbl.pack(side="left", padx=6)
        ttk.Button(bar, text="◂ Menú", command=self.to_menu).pack(side="right", padx=(8, 2))
        ttk.Button(bar, text="Pantalla completa", command=lambda: self.set_fullscreen(True)).pack(
            side="right", padx=4)
        self.mount_lbl = tk.Label(bar, text="montura: desconectada", bg=C_PANEL2, fg=C_MUTED, padx=10, pady=2)
        self.mount_lbl.pack(side="right", padx=4)
        self.cam_lbl = tk.Label(bar, text="cámara: desconectada", bg=C_PANEL2, fg=C_MUTED, padx=10, pady=2)
        self.cam_lbl.pack(side="right", padx=4)

    # ------------------------------------------------- conexion (modo completo)
    def find(self):
        """Busca en la red local y en las redes /24 del campo Raspberry (puede ser una red,
        p. ej. 192.168.201.0) y de las IPs donde ya se uso la Pi."""
        self.find_btn.configure(state="disabled")
        hints = [h for h in [self.host_var.get().strip()] + known_hints() if h]
        nets = nets_of(hints)
        self.hint_lbl.configure(text="buscando la Raspberry en la red local" + (
            " y en %s" % ", ".join(nets) if nets else "") + "…", fg=C_MUTED)
        self.cam.worker.submit(lambda: discover(targets=hints), self._found, self._found)

    def _found(self, res):
        self.find_btn.configure(state="normal")
        if isinstance(res, Exception) or not res:
            self.hint_lbl.configure(text=NOT_FOUND_HINT, fg=C_WARN)
            return
        ips = [s["ip"] for s in res]
        self.host_cb.configure(values=ips)
        self.host_var.set(ips[0])
        self.hint_lbl.configure(text="encontrada: %s (%s)" % (ips[0], res[0].get("host")), fg=C_OK)
        if not self.want:
            self.toggle_connection()      # encontrada: conectar sin otro clic

    def direct(self):
        DirectDialog.show(self.root, self._direct_found)

    def _direct_found(self, ip):
        if self.host_var.get().strip() != ip:     # estaba con la IP de antes
            if self.ptz.link.connected:
                self.ptz.toggle_conn()
            if self.cam.cam is not None:
                self.cam.disconnect()
        self.host_var.set(ip)
        self.hint_lbl.configure(text="encontrada: %s (conexión directa)" % ip, fg=C_OK)
        self.want = True                          # _header_tick conecta montura y camara
        self._ptz_try_t = self._cam_try_t = 0.0
        self._ptz_tries = 0
        save_cfg(host=ip)

    def _connected_any(self):
        cam = self.cam
        return self.want or cam.cam is not None or cam.connecting or self.ptz.link.connected

    def toggle_connection(self):
        cam, ptz = self.cam, self.ptz
        if self._connected_any():
            self.want = False
            if ptz.link.connected:
                ptz.toggle_conn()         # manda STOP y se desconecta
            if cam.cam is not None:
                cam.disconnect()
            self.hint_lbl.configure(text="")
            return
        host = self.host_var.get().strip()
        if not host:
            self.find()
            return
        self.want = True                  # _header_tick conecta montura y camara
        self._ptz_try_t = self._cam_try_t = 0.0
        self._ptz_tries = 0
        self.hint_lbl.configure(text="")
        save_cfg(host=host)

    def _connect_ptz(self):
        """La montura es la misma Raspberry; no espera a la camara."""
        host = self.host_var.get().strip()
        cam = self.cam.cam
        info = cam.server.get("ptz") if cam is not None else None
        port = info.get("port", PTZ_PORT) if isinstance(info, dict) else PTZ_PORT
        self.ptz.port_var.set("%s:%d" % (host, port))
        self._ptz_tries += 1
        self._probe_ptz_then_connect(quiet=self._ptz_tries > 1)   # el aviso, una sola vez

    def _header_tick(self):
        cam, ptz = self.cam, self.ptz
        try:
            self.cam_lbl.configure(text="cámara: " + cam.conn_lbl.cget("text"), fg=cam.conn_lbl.cget("fg"))
            self.mount_lbl.configure(text="montura: " + ptz.conn_lbl.cget("text"), fg=ptz.conn_lbl.cget("fg"))
            now = time.monotonic()
            host = self.host_var.get().strip()
            if self.want and host:
                if not ptz.link.connected and not self._ptz_probing and now - self._ptz_try_t > 5.0:
                    self._ptz_try_t = now
                    self._connect_ptz()
                if cam.cam is None and not cam.connecting and now - self._cam_try_t > 8.0:
                    self._cam_try_t = now   # reintento: conecta sola cuando se enchufa la camara
                    cam.host_var.set(host)
                    cam.connect()
                if cam.cam is None and ptz.link.connected and ptz.pong_ok:
                    self.hint_lbl.configure(text="la Raspberry no tiene la cámara conectada: la montura "
                                                 "funciona igual (reintento cada 8 s)", fg=C_WARN)
                elif cam.cam is not None and self.hint_lbl.cget("text").startswith("la Raspberry no tiene"):
                    self.hint_lbl.configure(text="")
            self.conn_btn.configure(text="Desconectar" if self._connected_any() else "Conectar")
        except tk.TclError:
            return
        self.root.after(250, self._header_tick)

    # ------------------------------------------------------- pantalla completa
    def set_fullscreen(self, on):
        if self.mode != "full" or bool(on) == self.fullscreen:
            return
        r, view = self.root, self.cam.canvas.master
        if on:
            self.fullscreen = True
            try:
                self._sash = self.paned.sash_coord(0)
            except tk.TclError:
                self._sash = None
            self._hidden = [w for w in self.cam_box.grid_slaves() if w is not view]
            for w in self._hidden:
                w.grid_remove()
            self._view_pad = view.grid_info()
            view.grid_configure(padx=0, pady=0)
            self.header.grid_remove()
            self.paned.paneconfigure(self.ptz_box, hide=True)
            r.attributes("-fullscreen", True)
            self._hud_create()
            self.cam.canvas.focus_set()
            self._hud_tick()
        else:
            self.fullscreen = False
            r.attributes("-fullscreen", False)
            self._hud_destroy()
            self.header.grid()
            for w in self._hidden:
                w.grid()
            view.grid_configure(padx=self._view_pad.get("padx", 0), pady=self._view_pad.get("pady", 0))
            self.paned.paneconfigure(self.ptz_box, hide=False)
            if self._sash:
                r.after(50, lambda: self.paned.sash_place(0, *self._sash))

    def _double_click(self, e):
        c = self.cam.canvas
        cur = c.find_withtag("current")
        if cur and HUD in c.gettags(cur[0]):
            return                        # doble clic sobre un boton de la pantalla
        self.set_fullscreen(not self.fullscreen)

    def _hud_create(self):
        c = self.cam.canvas
        self._hud_destroy()
        h = self._hud

        def text(**kw):
            return c.create_text(0, 0, tags=(HUD,), **kw)
        h["box"] = c.create_rectangle(0, 0, 0, 0, fill=C_HUD, outline=C_LINE, tags=(HUD,))
        h["pan_t"] = text(text="PAN X", fill=C_X, font=("Segoe UI", 9, "bold"), anchor="nw")
        h["pan"] = text(text="", fill=C_X, font=("Consolas", 22, "bold"), anchor="nw")
        h["tilt_t"] = text(text="TILT Y", fill=C_Y, font=("Segoe UI", 9, "bold"), anchor="nw")
        h["tilt"] = text(text="", fill=C_Y, font=("Consolas", 22, "bold"), anchor="nw")
        h["info"] = text(text="", fill=C_MUTED, font=("Segoe UI", 9), anchor="nw")
        h["hint"] = text(text="Flechas o mando: mover  ·  Espacio: STOP  ·  + / −: velocidad  ·  "
                              "Esc o doble clic: salir", fill=C_TEXT, font=("Segoe UI", 10), anchor="sw")
        h["hint_bg"] = c.create_rectangle(0, 0, 0, 0, fill=C_HUD, outline="", tags=(HUD,))
        self._btns = {}
        specs = (("up", "▲", C_Y, ("Y", +1)), ("down", "▼", C_Y, ("Y", -1)),
                 ("left", "◀", C_X, ("X", -1)), ("right", "▶", C_X, ("X", +1)),
                 ("stop", "STOP", C_TEXT, None), ("slow", "−", C_TEXT, None), ("fast", "+", C_TEXT, None))
        for name, glyph, color, jog in specs:
            tag = "hud_" + name
            fill = C_STOP if name == "stop" else C_PANEL2
            o = c.create_oval(0, 0, 0, 0, fill=fill, outline=C_LINE, width=2, tags=(HUD, tag))
            t = c.create_text(0, 0, text=glyph, fill=color, tags=(HUD, tag),
                              font=("Segoe UI", 10 if name == "stop" else 15, "bold"))
            self._btns[name] = (o, t, fill)
            c.tag_bind(tag, "<ButtonPress-1>", lambda e, n=name, j=jog: self._hud_press(n, j))
        c.tag_lower(h["hint_bg"], h["hint"])
        self._hud_place()

    def _hud_destroy(self):
        c = self.cam.canvas
        c.delete(HUD)
        self._hud = {}
        self._btns = {}

    def _hud_place(self):
        if not self._hud:
            return
        c, h = self.cam.canvas, self._hud
        w, ht = c.winfo_width(), c.winfo_height()
        x1, y1 = w - 300, 48   # bajo la esquina: ahi suelen ir los indicadores de FPS/GPU del sistema
        c.coords(h["box"], x1, y1, w - 14, y1 + 100)
        c.coords(h["pan_t"], x1 + 16, y1 + 10)
        c.coords(h["pan"], x1 + 16, y1 + 26)
        c.coords(h["tilt_t"], x1 + 150, y1 + 10)
        c.coords(h["tilt"], x1 + 150, y1 + 26)
        c.coords(h["info"], x1 + 16, y1 + 72)
        c.coords(h["hint"], 18, ht - 16)
        bx = c.bbox(h["hint"])
        if bx:
            c.coords(h["hint_bg"], bx[0] - 8, bx[1] - 5, bx[2] + 8, bx[3] + 5)
        cx, cy, off = w - 120, ht - 130, 66
        pos = {"up": (cx, cy - off, 30), "down": (cx, cy + off, 30), "left": (cx - off, cy, 30),
               "right": (cx + off, cy, 30), "stop": (cx, cy, 28),
               "slow": (cx - off - 92, cy + off, 22), "fast": (cx - off - 40, cy + off, 22)}
        for name, (x, y, rad) in pos.items():
            o, t, _ = self._btns[name]
            c.coords(o, x - rad, y - rad, x + rad, y + rad)
            c.coords(t, x, y)
        c.tag_raise(HUD)

    def _hud_press(self, name, jog):
        o, _, _ = self._btns[name]
        self.cam.canvas.itemconfigure(o, fill=C_LINE)
        if jog is not None:
            self.ptz.jog_start(jog[0], jog[1], "mouse")
        elif name == "stop":
            self.ptz.stop_all()
        else:
            self._speed_step(+1 if name == "fast" else -1)

    def _hud_release(self, _e=None):
        for o, _, fill in self._btns.values():
            self.cam.canvas.itemconfigure(o, fill=fill)
        if self.ptz is not None:
            for axis in "XY":
                self.ptz.jog_stop(axis, "mouse")

    def _hud_tick(self):
        if not self.fullscreen or not self._hud:
            return
        p, c, h = self.ptz, self.cam.canvas, self._hud
        try:
            c.itemconfigure(h["pan"], text="%+.1f°" % PTZ.us_to_deg(p.cur["X"], "X"))
            c.itemconfigure(h["tilt"], text="%+.1f°" % PTZ.us_to_deg(p.cur["Y"], "Y"))
            ok = p.link.connected and p.pong_ok
            moving = any(p.att[a] and p.cur[a] != p.tgt[a] for a in "XY")
            c.itemconfigure(h["info"], fill=C_OK if ok else C_WARN, text="%.1f °/s  ·  montura %s%s" % (
                p.speed_var.get(), "conectada" if ok else "sin conexión", "  ·  moviendo" if moving else ""))
            c.tag_raise(HUD)
        except tk.TclError:
            return
        self.root.after(100, self._hud_tick)

    def _speed_step(self, step):
        vals = [v for _, v in PTZ.SPEED_PRESETS]
        cur = self.ptz.speed_var.get()
        if step > 0:
            nxt = next((v for v in vals if v > cur + 1e-6), vals[-1])
        else:
            nxt = next((v for v in reversed(vals) if v < cur - 1e-6), vals[0])
        self.ptz.set_speed(nxt)

    def _speed_key(self, e, step):
        if self.fullscreen and not isinstance(e.widget, (tk.Entry, tk.Spinbox, ttk.Combobox)):
            self._speed_step(step)

    # ---------------------------------------------------------------- cerrar
    def to_menu(self):
        self.close("menu")

    def close(self, nxt=None):
        self.next = nxt
        if self.fullscreen:
            try:
                self.root.attributes("-fullscreen", False)
            except tk.TclError:
                pass
        # cada interfaz se cierra a su manera: la montura manda STOP, la camara
        # termina la grabacion y suelta la conexion
        cancel_afters(self.root)
        for gui, method in ((self.ptz, "_on_close"), (self.cam, "on_close")):
            if gui is not None:
                try:
                    getattr(gui, method)()
                except Exception:  # noqa: BLE001
                    traceback.print_exc()
        cancel_afters(self.root)
        try:
            self.root.destroy()
        except tk.TclError:
            pass


# ----------------------------------------------------------------------------
# Prueba automatica (--selftest): recorre el menu y los tres modos
# ----------------------------------------------------------------------------

class SelfTest:
    TIMEOUT_S = 240

    def __init__(self, host):
        self.host = host
        self.results = []
        self.win = None
        self.finished = False
        self.exit_code = 1
        self.t0 = time.monotonic()
        self.steps = self._script()

    def attach(self, win):
        self.win = win
        win.root.after(400, lambda: self._tick(win))

    def check(self, name, ok, detail=""):
        self.results.append((name, bool(ok)))
        print("  [%s] %s%s" % ("OK " if ok else "FALLA", name, (" - " + detail) if detail else ""), flush=True)
        return ok

    def _tick(self, win):
        if self.win is not win or self.finished:
            return
        if time.monotonic() - self.t0 > self.TIMEOUT_S:
            self.check("tiempo agotado", False)
            self._finish()
            return
        try:
            next(self.steps)
        except StopIteration:
            self._finish()
            return
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.check("excepción: %s" % e, False)
            self._finish()
            return
        if self.win is win:
            try:
                win.root.after(40, lambda: self._tick(win))
            except tk.TclError:
                pass

    def _finish(self):
        if self.finished:
            return
        self.finished = True
        bad = [n for n, ok in self.results if not ok]
        print("\nRESUMEN: %d pruebas, %d fallas%s" % (len(self.results), len(bad),
                                                    (": " + "; ".join(bad)) if bad else ""), flush=True)
        self.exit_code = 1 if bad else 0
        w = self.win
        if isinstance(w, ModeWindow):
            w.root.after(50, w.close)
        elif w is not None:
            w.root.after(50, w.root.destroy)

    # ayudas para el guion
    @staticmethod
    def wait(cond, timeout):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            try:
                if cond():
                    return True
            except Exception:  # noqa: BLE001
                pass
            yield
        return False

    @staticmethod
    def sleep(secs):
        t0 = time.monotonic()
        while time.monotonic() - t0 < secs:
            yield

    @staticmethod
    def key(widget, keysym, press=True):
        widget.focus_force()
        widget.update()
        widget.event_generate("<KeyPress>" if press else "<KeyRelease>", keysym=keysym, when="tail")

    @staticmethod
    def click(widget, x, y, double=False):
        for _ in range(2 if double else 1):
            widget.event_generate("<ButtonPress-1>", x=x, y=y, when="tail")
            widget.event_generate("<ButtonRelease-1>", x=x, y=y, when="tail")

    def _settled(self, p, axis):
        return p.cur[axis] == p.tgt[axis]

    def _jog_by_key(self, label, widget, p, keysym, axis):
        """Mantiene una flecha 0,6 s: el eje se tiene que mover y frenar al soltar."""
        v0 = p.cur[axis]
        self.key(widget, keysym, True)
        moved = yield from self.wait(lambda: p.cur[axis] != v0, 3)
        yield from self.sleep(0.4)
        self.key(widget, keysym, False)
        yield from self.sleep(0.6)
        v1 = p.cur[axis]
        yield from self.sleep(0.4)
        self.check(label, moved and p.cur[axis] == v1 and p.jogging[axis] == 0,
                   "%s %d -> %d us" % (axis, v0, p.cur[axis]))

    def _script(self):
        print("SELFTEST: servidor %s" % self.host, flush=True)

        # 1) menu -> Montura
        ok = yield from self.wait(lambda: isinstance(self.win, Menu), 5)
        self.check("menú de inicio con los tres modos", ok and set(self.win.cards) == {"ptz", "cam", "full"})
        card = self.win.cards["ptz"]
        self.click(card, 30, 30)               # como un clic del usuario sobre la tarjeta
        ok = yield from self.wait(lambda: isinstance(self.win, ModeWindow) and self.win.mode == "ptz", 5)
        self.check("clic en Montura abre ese modo", ok)
        if not ok:
            return
        w, p = self.win, self.win.ptz
        yield from self.sleep(0.6)
        self.check("la ventana toma el teclado al abrir", w.root.focus_get() is not None,
                   repr(w.root.focus_get()))
        vals = list(p.port_cb.cget("values") or ())
        self.check("sin puertos COM (solo red)", all(PTZ.is_net_port(v) for v in vals), str(vals))
        ok = yield from self.wait(lambda: p.pong_ok, 6)    # se conecta sola si la Pi contesta
        self.check("montura conectada sola al abrir", ok, p.port_var.get())
        if not ok and not p.link.connected:
            p.port_var.set("%s:%d" % (self.host, PTZ_PORT))
            p.toggle_conn()
            yield from self.wait(lambda: p.pong_ok, 6)
        yield from self.sleep(0.6)
        self.check("diales de los ejes dibujados", bool(p.dial_x.find_all()) and bool(p.dial_y.find_all()))
        p.port_cb.focus_force()             # como si el usuario hubiera usado la lista Puerto
        yield from self.sleep(0.2)
        self.click(p.dial_x, 40, 40)
        yield from self.sleep(0.3)
        self.check("clic fuera de un campo devuelve las flechas a la montura",
                   w.root.focus_get() is w.root, repr(w.root.focus_get()))
        yield from self._jog_by_key("flecha derecha mueve X y frena al soltar", w.root, p, "Right", "X")
        p.center()
        yield from self.wait(lambda: self._settled(p, "X") and self._settled(p, "Y"), 6)
        w.to_menu()
        yield

        # 2) menu -> Camara
        ok = yield from self.wait(lambda: isinstance(self.win, Menu), 5)
        self.check("vuelve al menú", ok)
        self.win.choose("cam")
        ok = yield from self.wait(lambda: isinstance(self.win, ModeWindow) and self.win.mode == "cam", 5)
        self.check("abre el modo Cámara", ok)
        if not ok:
            return
        g = self.win.cam
        ok = yield from self.wait(lambda: g.cam is not None and g._shown is not None, 15)
        self.check("cámara conectada y con video", ok, g.conn_lbl.cget("text"))
        yield from self.sleep(2)
        self.check("vista fluida", g.disp_fps() > 4, "%.1f fps" % g.disp_fps())
        self.win.to_menu()
        yield

        # 3) menu -> Montura completa
        ok = yield from self.wait(lambda: isinstance(self.win, Menu), 5)
        self.win.choose("full")
        ok = yield from self.wait(lambda: isinstance(self.win, ModeWindow) and self.win.mode == "full", 5)
        self.check("abre Montura completa", ok)
        if not ok:
            return
        w = self.win
        g, p, c = w.cam, w.ptz, w.cam.canvas
        if not g.host_var.get():
            w.host_var.set(self.host)
            w.toggle_connection()
        ok = yield from self.wait(lambda: g._shown is not None and p.pong_ok, 20)
        self.check("un solo Conectar: cámara y montura", ok, "%s / %s" % (
            w.cam_lbl.cget("text"), w.mount_lbl.cget("text")))
        self.check("una sola barra de conexión", w.cam_box.top_bar() is None and w.ptz_box.top_bar() is None
                   and w.header.winfo_ismapped())
        yield from self.sleep(0.5)
        yield from self._jog_by_key("flecha izquierda mueve X (ventana normal)", c, p, "Left", "X")

        # doble clic -> pantalla completa
        cw, ch = c.winfo_width(), c.winfo_height()
        self.click(c, cw // 3, ch // 3, double=True)
        ok = yield from self.wait(lambda: w.fullscreen, 3)
        yield from self.sleep(1.2)
        fs = str(w.root.attributes("-fullscreen")) in ("1", "True", "true")
        self.check("doble clic: pantalla completa", ok and fs and c.winfo_width() >= 0.9 * w.root.winfo_screenwidth(),
                   "lienzo %dx%d, pantalla %dx%d" % (c.winfo_width(), c.winfo_height(),
                                                     w.root.winfo_screenwidth(), w.root.winfo_screenheight()))
        self.check("se ocultan los paneles", not w.header.winfo_ismapped() and not w.ptz_box.winfo_ismapped()
                   and not g.side.winfo_ismapped())
        s0 = g._shown.seq if g._shown else -1
        ok = yield from self.wait(lambda: g._shown is not None and g._shown.seq > s0 + 5, 4)
        self.check("el video sigue en pantalla completa", ok)
        self.check("datos de la montura sobre la imagen", bool(c.find_withtag(HUD))
                   and c.itemcget(w._hud["pan"], "text").endswith("°"), c.itemcget(w._hud["pan"], "text"))
        yield from self._jog_by_key("flecha arriba mueve Y en pantalla completa", c, p, "Up", "Y")

        o = w._btns["left"][0]
        x1, y1, x2, y2 = c.coords(o)
        bx, by = int((x1 + x2) / 2), int((y1 + y2) / 2)
        x0 = p.cur["X"]
        c.event_generate("<Motion>", x=bx, y=by, when="tail")
        c.event_generate("<ButtonPress-1>", x=bx, y=by, when="tail")
        moved = yield from self.wait(lambda: p.cur["X"] != x0, 3)
        yield from self.sleep(0.3)
        c.event_generate("<ButtonRelease-1>", x=bx, y=by, when="tail")
        yield from self.sleep(0.6)
        v1 = p.cur["X"]
        yield from self.sleep(0.4)
        self.check("botón ◀ sobre la imagen mueve X y frena al soltar",
                   moved and p.cur["X"] == v1 and p.jogging["X"] == 0, "X %d -> %d us" % (x0, p.cur["X"]))

        sp0 = p.speed_var.get()
        self.key(c, "plus")
        yield from self.sleep(0.3)
        sp1 = p.speed_var.get()
        self.key(c, "minus")
        yield from self.sleep(0.3)
        self.check("+ / − cambian la velocidad", sp1 > sp0 and abs(p.speed_var.get() - sp0) < 1e-6,
                   "%.1f -> %.1f -> %.1f °/s" % (sp0, sp1, p.speed_var.get()))

        self.key(c, "Escape")
        ok = yield from self.wait(lambda: not w.fullscreen, 3)
        yield from self.sleep(1.0)
        self.check("Esc vuelve a la ventana con los paneles", ok and w.header.winfo_ismapped()
                   and w.ptz_box.winfo_ismapped() and g.side.winfo_ismapped() and not c.find_withtag(HUD))
        p.center()
        yield from self.wait(lambda: self._settled(p, "X") and self._settled(p, "Y"), 6)


def main(argv=None):
    ap = argparse.ArgumentParser(description="UFOMOUNT: montura y cámara con la Raspberry")
    ap.add_argument("--mode", choices=("ptz", "cam", "full"), help="abre directo ese modo (sin menú)")
    ap.add_argument("--host", help="IP de la Raspberry")
    ap.add_argument("--fullscreen", action="store_true", help="con --mode full: arranca en pantalla completa")
    ap.add_argument("--simulate", action="store_true", help="levanta un servidor simulado local y se conecta")
    ap.add_argument("--selftest", action="store_true", help="prueba automática de los tres modos y sale")
    # uso interno: el ayudante con permisos de administrador de la conexion directa
    ap.add_argument("--direct-helper", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--direct-port", type=int, default=0, help=argparse.SUPPRESS)
    ap.add_argument("--direct-token", default="", help=argparse.SUPPRESS)
    ap.add_argument("--direct-if", type=int, default=-1, help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.direct_helper:
        sys.exit(direct_link.run_helper(a.direct_port, a.direct_token, a.direct_if))
    # en el ejecutable sin consola no hay stdout/stderr: print fallaria
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    for stream in (sys.stdout, sys.stderr):   # la consola de Windows no tiene "◀" ni "°"
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    if a.simulate or a.selftest:
        no_save_prefs()
    sim = CAM.start_simulator() if a.simulate else None
    host = "127.0.0.1" if a.simulate else a.host
    test = SelfTest(host or last_host()) if a.selftest else None
    mode = None if a.selftest else a.mode
    try:
        while True:
            if mode is None:
                menu = Menu(load_cfg().get("last_mode"), test)
                menu.root.mainloop()
                mode = menu.choice
                if mode is None:
                    break
            win = ModeWindow(mode, host, test)
            if a.fullscreen and mode == "full" and test is None:
                win.root.after(1500, lambda w=win: w.set_fullscreen(True))
            win.root.mainloop()
            if win.next != "menu":
                break
            mode = None
    finally:
        DIRECT.stop()                # el ayudante deja el puerto como estaba
        if sim is not None:
            sim.terminate()
            sim.wait(10)
    sys.exit(test.exit_code if test is not None else 0)


if __name__ == "__main__":
    main()
