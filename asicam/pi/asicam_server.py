#!/usr/bin/env python3
"""Servidor asicam: la Raspberry como relay de video de una camara ZWO ASI.

Captura cuadros crudos (RAW8/RAW16) con el SDK de ZWO y los reenvia sin
comprimir por TCP. El procesamiento se hace en el PC que se conecta.

Puertos (por defecto):
    5300/TCP  control: lineas JSON (hello, info, get, set, status)
    5301/TCP  datos:   cuadros crudos con cabecera binaria
    5300/UDP  busqueda: responde a "ASICAM?"

La camara se abre solo mientras hay clientes de datos conectados; sin
clientes se suelta a los pocos segundos (queda libre para PHD2/INDI).

Uso:
    python3 asicam_server.py                      # SDK por defecto
    python3 asicam_server.py --sdk /ruta/libASICamera2.so.1.39
    sudo python3 asicam_server.py --rt --config /home/astroberry/.config/asicam/settings-rt.json

Compatible con Python 3.7. Protocolo completo en asicam/README.md.
"""

import argparse
import ctypes
import json
import logging
import os
import queue
import select
import signal
import socket
import socketserver
import struct
import sys
import threading
import time

import zwo_sdk as Z

VERSION = "0.1.0"
CTRL_PORT = 5300
DATA_PORT = 5301
DISCOVERY_MSG = b"ASICAM?"

# Cabecera de cada cuadro (little-endian, 48 bytes). Ver README.
FRAME_MAGIC = b"ASIF"
FRAME_VERSION = 1
FRAME_HDR = struct.Struct("<4sHHIdIHHHBBIHHh2xI")
FLAG_AUTO_EXPOSURE = 1
FLAG_AUTO_GAIN = 2

SDK_CANDIDATES = [
    "/home/astroberry/scripts/ASI_linux_mac_SDK_V1.39/lib/armv7/libASICamera2.so.1.39",
    "libASICamera2.so",
]

# Ajustes: clave -> (control del SDK, clave "auto" asociada o None)
CONTROL_KEYS = {
    "gain": (Z.GAIN, "auto_gain"),
    "exposure_us": (Z.EXPOSURE, "auto_exposure"),
    "offset": (Z.OFFSET, None),
    "bandwidth": (Z.BANDWIDTH, "auto_bandwidth"),
    "flip": (Z.FLIP, None),
    "high_speed": (Z.HIGH_SPEED_MODE, None),
    "gamma": (Z.GAMMA, None),
    "wb_r": (Z.WB_R, "auto_wb"),
    "wb_b": (Z.WB_B, "auto_wb"),
    "auto_max_gain": (Z.AUTO_MAX_GAIN, None),
    "auto_max_exp_ms": (Z.AUTO_MAX_EXP, None),
    "auto_target": (Z.AUTO_TARGET_BRIGHTNESS, None),
}
AUTO_KEYS = {"auto_gain", "auto_exposure", "auto_bandwidth", "auto_wb"}
GEOMETRY_KEYS = {"image_type", "bin", "width", "height", "start_x", "start_y"}

# None = usar el valor por defecto de la camara
DEFAULT_SETTINGS = {
    "image_type": "RAW8",   # RAW8 | RAW16
    "bin": 1,
    "width": 0,             # 0 = ancho maximo para ese bin
    "height": 0,            # 0 = alto maximo para ese bin
    "start_x": -1,          # -1 = centrado
    "start_y": -1,
    "exposure_us": 10000,
    "auto_exposure": False,
    "gain": 0,
    "auto_gain": False,
    "offset": None,
    "bandwidth": 60,        # 60 % estable sin tiempo real; 70 % con --rt
    "auto_bandwidth": False,
    "high_speed": 0,        # en la ASI220 Mini el modo High Speed la limita a ~10 fps
    "flip": 0,
    "gamma": None,
    "wb_r": None,
    "wb_b": None,
    "auto_wb": False,
    "auto_max_gain": None,
    "auto_max_exp_ms": None,
    "auto_target": None,
}

FAILS_BEFORE_RESTART = 3     # lecturas fallidas seguidas antes de reiniciar el video
IDLE_CLOSE_S = 5.0           # sin clientes: soltar la camara tras estos segundos
REOPEN_WAIT_S = 2.0          # espera entre intentos de abrir la camara
TEMP_EVERY_S = 2.0

log = logging.getLogger("asicam")


class SettingsError(ValueError):
    pass


def _as_bool(key, v):
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    raise SettingsError("%s debe ser true o false" % key)


def _as_int(key, v):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v):
        raise SettingsError("%s debe ser un entero" % key)
    return int(v)


class Settings:
    """Ajustes pedidos por los clientes, guardados en JSON."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.values = dict(DEFAULT_SETTINGS)
        self.version = 0          # sube con cada cambio
        self.geometry_version = 0  # sube con cambios que obligan a reiniciar el video
        if path and os.path.exists(path):
            try:
                with open(path) as f:
                    saved = json.load(f)
                for k, v in saved.items():
                    if k in DEFAULT_SETTINGS:
                        self.values[k] = v
                log.info("ajustes cargados de %s", path)
            except (OSError, ValueError) as e:
                log.warning("no pude leer %s: %s (uso los de fabrica)", path, e)

    def snapshot(self):
        with self.lock:
            return dict(self.values), self.version, self.geometry_version

    def validate(self, changes, camera_info, controls):
        """Revisa un diccionario de cambios. Devuelve los valores normalizados."""
        if not isinstance(changes, dict) or not changes:
            raise SettingsError("settings debe ser un objeto con al menos un ajuste")
        with self.lock:
            current = dict(self.values)
        out = {}
        for key, v in changes.items():
            if key not in DEFAULT_SETTINGS:
                raise SettingsError("ajuste desconocido: %s" % key)
            if key in AUTO_KEYS:
                out[key] = _as_bool(key, v)
                continue
            if key == "image_type":
                if v not in ("RAW8", "RAW16"):
                    raise SettingsError("image_type debe ser RAW8 o RAW16")
                if camera_info and v not in camera_info["formats"]:
                    raise SettingsError("la camara no soporta %s" % v)
                out[key] = v
                continue
            if v is None and DEFAULT_SETTINGS[key] is None:
                out[key] = None
                continue
            iv = _as_int(key, v)
            if key == "bin":
                if camera_info and iv not in camera_info["bins"]:
                    raise SettingsError("bin %d no soportado (opciones: %s)" % (iv, camera_info["bins"]))
            elif key in ("width", "height"):
                if iv < 0:
                    raise SettingsError("%s no puede ser negativo" % key)
            elif key in ("start_x", "start_y"):
                if iv < -1:
                    raise SettingsError("%s debe ser -1 (centrado) o >= 0" % key)
            elif key == "high_speed":
                iv = 1 if iv else 0
            if key in CONTROL_KEYS and controls is not None:
                ctl = CONTROL_KEYS[key][0]
                caps = controls.get(ctl)
                if caps is None:
                    if iv == current.get(key):  # reenviar lo mismo que ya hay no es error
                        out[key] = iv
                        continue
                    raise SettingsError("la camara no tiene el control %s" % key)
                if not (caps["min"] <= iv <= caps["max"]):
                    raise SettingsError("%s fuera de rango (%d a %d)" % (key, caps["min"], caps["max"]))
            out[key] = iv
        # auto solo si el control lo soporta
        if controls is not None:
            for key, (ctl, auto_key) in CONTROL_KEYS.items():
                if auto_key and out.get(auto_key) and ctl in controls and not controls[ctl]["auto"]:
                    raise SettingsError("%s no admite modo automatico" % key)
        return out

    def apply(self, changes):
        with self.lock:
            geometry = any(k in GEOMETRY_KEYS and self.values.get(k) != v for k, v in changes.items())
            self.values.update(changes)
            self.version += 1
            if geometry:
                self.geometry_version += 1
            data = dict(self.values)
        if self.path:
            try:
                d = os.path.dirname(self.path)
                if d:
                    os.makedirs(d, exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(data, f, indent=1, sort_keys=True)
                os.replace(tmp, self.path)
            except OSError as e:
                log.warning("no pude guardar ajustes en %s: %s", self.path, e)
        return data


def compute_geometry(s, info):
    """Ancho, alto, bin, tipo y posicion validos para el SDK a partir de los ajustes."""
    b = s["bin"] if s["bin"] in info["bins"] else 1
    maxw, maxh = info["max_width"] // b, info["max_height"] // b
    w = s["width"] or maxw
    h = s["height"] or maxh
    w = min(w, maxw) // 8 * 8      # el SDK pide ancho multiplo de 8
    h = min(h, maxh) // 2 * 2      # y alto par
    w, h = max(w, 8), max(h, 2)
    x = s["start_x"] if s["start_x"] >= 0 else (maxw - w) // 2
    y = s["start_y"] if s["start_y"] >= 0 else (maxh - h) // 2
    x = min(max(x, 0), maxw - w) // 2 * 2
    y = min(max(y, 0), maxh - h) // 2 * 2
    t = Z.IMG_RAW16 if s["image_type"] == "RAW16" else Z.IMG_RAW8
    return w, h, b, t, x, y


class FrameClient:
    """Un cliente de datos: cola propia y un hilo que le envia los cuadros."""

    def __init__(self, sock, addr, on_close):
        self.sock = sock
        self.addr = addr
        self.q = queue.Queue(maxsize=3)
        self.sent = 0
        self.dropped = 0
        self.alive = True
        self._on_close = on_close
        self.thread = threading.Thread(target=self._run, name="datos-%s" % addr[0], daemon=True)
        self.thread.start()

    def offer(self, header, payload):
        if not self.alive:
            return
        try:
            self.q.put_nowait((header, payload))
        except queue.Full:
            self.dropped += 1

    def _peer_closed(self):
        """El cliente no manda nada por este canal: si hay algo para leer, es que cerro."""
        try:
            r, _, _ = select.select([self.sock], [], [], 0)
            return bool(r) and self.sock.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    def _run(self):
        try:
            while self.alive:
                try:
                    item = self.q.get(timeout=1.0)
                except queue.Empty:
                    if self._peer_closed():
                        log.info("cliente de datos %s cerro la conexion", self.addr[0])
                        break
                    continue
                if item is None:
                    break
                self.sock.sendall(item[0])
                self.sock.sendall(item[1])
                self.sent += 1
        except OSError as e:
            log.info("cliente de datos %s se fue (%s)", self.addr[0], e)
        finally:
            self.alive = False
            try:
                self.sock.close()
            except OSError:
                pass
            self._on_close(self)

    def close(self):
        self.alive = False
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class Capturer(threading.Thread):
    """Hilo duenio de la camara: abre, ajusta, captura y reparte cuadros."""

    def __init__(self, sdk, settings, camera_index=0, realtime=False):
        super().__init__(name="captura", daemon=True)
        self.sdk = sdk
        self.settings = settings
        self.camera_index = camera_index
        self.realtime = realtime
        self.stop_event = threading.Event()
        self.clients_lock = threading.Lock()
        self.clients = []
        self.cam = None
        self.camera_info = None      # se conserva aunque la camara se cierre
        self.controls = None
        self.lock = threading.Lock()  # protege el estado de abajo
        self.state = "inactiva"
        self.last_error = ""
        self.frames = 0
        self.restarts = 0
        self.reopens = 0
        self.dropped_sdk = 0
        self.temperature = None
        self.actual = {}
        self.geometry = None
        self.rt_active = False
        self._fps_times = []

    # ---- clientes ----
    def add_client(self, client):
        with self.clients_lock:
            self.clients.append(client)

    def remove_client(self, client):
        with self.clients_lock:
            if client in self.clients:
                self.clients.remove(client)

    def client_count(self):
        with self.clients_lock:
            return sum(1 for c in self.clients if c.alive)

    # ---- estado para el canal de control ----
    def status(self):
        with self.lock:
            now = time.monotonic()
            times = [t for t in self._fps_times if now - t <= 3.0]
            fps = (len(times) - 1) / (times[-1] - times[0]) if len(times) > 1 and times[-1] > times[0] else 0.0
            with self.clients_lock:
                cl = [{"addr": c.addr[0], "sent": c.sent, "dropped": c.dropped} for c in self.clients]
            return {
                "state": self.state,
                "camera": self.camera_info["name"] if self.camera_info else None,
                "fps": round(fps, 2),
                "frames": self.frames,
                "dropped_sdk": self.dropped_sdk,
                "restarts": self.restarts,
                "reopens": self.reopens,
                "temperature_c": self.temperature,
                "geometry": self.geometry,
                "actual": dict(self.actual),
                "clients": cl,
                "realtime": self.rt_active,
                "error": self.last_error,
            }

    def _set_state(self, state, error=None):
        with self.lock:
            if state != self.state:
                log.info("estado: %s%s", state, (" (%s)" % error) if error else "")
            self.state = state
            if error is not None:
                self.last_error = error

    # ---- camara ----
    def probe(self):
        """Abre la camara un momento para conocer sus datos y la cierra."""
        try:
            cam = self.sdk.open(self.camera_index)
        except Z.ASIError as e:
            log.warning("no hay camara al iniciar: %s", e)
            return False
        try:
            self.camera_info, self.controls = cam.info, cam.controls
            log.info("camara: %s %dx%d formatos=%s bins=%s", cam.info["name"], cam.info["max_width"],
                     cam.info["max_height"], cam.info["formats"], cam.info["bins"])
        finally:
            cam.close()
        return True

    def _open(self):
        self._set_state("abriendo")
        cam = self.sdk.open(self.camera_index)
        self.cam = cam
        self.camera_info, self.controls = cam.info, cam.controls
        with self.lock:
            self.reopens += 1
        log.info("camara abierta: %s", cam.info["name"])

    def _close(self):
        if self.cam is not None:
            try:
                self.cam.close()
            except Exception as e:  # noqa: BLE001 - cerrar nunca debe tumbar el hilo
                log.warning("al cerrar la camara: %s", e)
            self.cam = None
            log.info("camara liberada")

    def _apply_controls(self, s):
        cam = self.cam
        for key, (ctl, auto_key) in CONTROL_KEYS.items():
            if not cam.has(ctl) or not cam.controls[ctl]["writable"]:
                continue
            v = s.get(key)
            auto = bool(s.get(auto_key)) if auto_key else False
            if v is None:
                if not auto:
                    continue
                v = cam.get_control(ctl)[0]
            try:
                cam.set_control(ctl, v, auto)
            except Z.ASIError as e:
                log.warning("%s", e)

    def _apply_geometry(self, s):
        w, h, b, t, x, y = compute_geometry(s, self.cam.info)
        self.cam.set_roi(w, h, b, t, x, y)
        roi = self.cam.get_roi()
        size = roi["width"] * roi["height"] * Z.BYTES_PER_PIXEL[roi["img_type"]]
        with self.lock:
            self.geometry = {"width": roi["width"], "height": roi["height"], "bin": roi["bin"],
                             "image_type": Z.IMG_NAMES[roi["img_type"]],
                             "start_x": roi["start_x"], "start_y": roi["start_y"]}
        log.info("geometria: %dx%d bin%d %s en (%d,%d)", roi["width"], roi["height"], roi["bin"],
                 Z.IMG_NAMES[roi["img_type"]], roi["start_x"], roi["start_y"])
        return roi, (ctypes.c_ubyte * size)()

    def _restart_video(self):
        try:
            self.cam.stop_video()
        except Z.ASIError:
            pass
        self.cam.start_video()
        with self.lock:
            self.restarts += 1
        return self.cam.dropped_frames()

    def _enable_realtime(self):
        if not self.realtime:
            return
        try:
            os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(80))
            self.rt_active = True
            log.info("hilo de captura en tiempo real (SCHED_FIFO 80)")
        except (AttributeError, PermissionError, OSError) as e:
            log.warning("no pude activar tiempo real (%s): sigo con prioridad normal", e)

    def run(self):
        self._enable_realtime()
        idle_since = None
        while not self.stop_event.is_set():
            if self.client_count() == 0:
                if self.cam is not None:
                    if idle_since is None:
                        idle_since = time.monotonic()
                        try:
                            self.cam.stop_video()
                        except Z.ASIError:
                            pass
                        self._set_state("en espera")
                    elif time.monotonic() - idle_since >= IDLE_CLOSE_S:
                        self._close()
                        self._set_state("inactiva")
                self.stop_event.wait(0.2)
                continue
            idle_since = None
            try:
                self._capture_session()
            except Z.ASIError as e:
                if str(e) != self.last_error:
                    log.warning("captura interrumpida: %s", e)
                self._set_state("error", str(e))
                self._close()
                self.stop_event.wait(REOPEN_WAIT_S)
            except Exception as e:  # noqa: BLE001 - el hilo no debe morir
                self._set_state("error", repr(e))
                log.exception("error inesperado en la captura")
                self._close()
                self.stop_event.wait(REOPEN_WAIT_S)
        self._close()

    def _capture_session(self):
        """Captura mientras haya clientes. Sale (con la camara abierta) si no quedan."""
        if self.cam is None:
            self._open()
        s, sver, gver = self.settings.snapshot()
        try:
            self.cam.stop_video()
        except Z.ASIError:
            pass
        roi, buf = self._apply_geometry(s)
        self._apply_controls(s)
        self.cam.start_video()
        self._set_state("capturando", "")
        fails = 0
        last_temp = 0.0
        last_dropped = self.cam.dropped_frames()
        while not self.stop_event.is_set():
            if self.client_count() == 0:
                return
            s2, sver2, gver2 = self.settings.snapshot()
            if gver2 != gver:
                self.cam.stop_video()
                roi, buf = self._apply_geometry(s2)
                self._apply_controls(s2)
                self.cam.start_video()
                last_dropped = self.cam.dropped_frames()
                fails = 0
            elif sver2 != sver:
                self._apply_controls(s2)
            s, sver, gver = s2, sver2, gver2
            exp_ms = (s.get("auto_max_exp_ms") or 1000) if s.get("auto_exposure") else s["exposure_us"] / 1000.0
            wait_ms = int(exp_ms * 2 + 500)
            rc = self.cam.read_frame(buf, wait_ms)
            now = time.time()
            if rc != Z.OK:
                if rc in (Z.ERR_CAMERA_CLOSED, Z.ERR_CAMERA_REMOVED):
                    raise Z.ASIError("leer cuadro", rc)
                fails += 1
                if fails >= FAILS_BEFORE_RESTART:
                    log.warning("%d lecturas fallidas seguidas (ultimo codigo %d): reinicio el video", fails, rc)
                    last_dropped = self._restart_video()
                    fails = 0
                continue
            fails = 0
            exposure = s["exposure_us"]
            gain = s["gain"] if s["gain"] is not None else 0
            if s.get("auto_exposure"):
                exposure = self.cam.get_control(Z.EXPOSURE)[0]
            if s.get("auto_gain"):
                gain = self.cam.get_control(Z.GAIN)[0]
            mono = time.monotonic()
            if mono - last_temp >= TEMP_EVERY_S and self.cam.has(Z.TEMPERATURE):
                last_temp = mono
                try:
                    self.temperature = self.cam.get_control(Z.TEMPERATURE)[0] / 10.0
                except Z.ASIError:
                    pass
            cur_dropped = self.cam.dropped_frames()
            new_drops = cur_dropped - last_dropped if cur_dropped >= last_dropped else 0
            last_dropped = cur_dropped
            with self.lock:
                self.frames += 1
                seq = self.frames
                self.dropped_sdk += new_drops
                self.actual = {"exposure_us": exposure, "gain": gain}
                self._fps_times.append(mono)
                if len(self._fps_times) > 200:
                    del self._fps_times[:100]
                restarts = self.restarts
            flags = (FLAG_AUTO_EXPOSURE if s.get("auto_exposure") else 0) | \
                    (FLAG_AUTO_GAIN if s.get("auto_gain") else 0)
            temp10 = int(round(self.temperature * 10)) if self.temperature is not None else -32768
            payload = bytes(memoryview(buf))
            header = FRAME_HDR.pack(FRAME_MAGIC, FRAME_VERSION, FRAME_HDR.size, seq & 0xFFFFFFFF, now,
                                    int(exposure) & 0xFFFFFFFF, int(gain) & 0xFFFF,
                                    roi["width"], roi["height"], roi["bin"], roi["img_type"],
                                    self.dropped_sdk & 0xFFFFFFFF, restarts & 0xFFFF, flags,
                                    max(-32768, min(32767, temp10)), len(payload))
            with self.clients_lock:
                clients = list(self.clients)
            for c in clients:
                c.offer(header, payload)


# ---------------- canal de control ----------------

class ControlHandler(socketserver.StreamRequestHandler):
    def handle(self):
        srv = self.server.app
        peer = self.client_address[0]
        log.info("cliente de control %s conectado", peer)
        try:
            while True:
                line = self.rfile.readline(65536)
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                req = None
                try:
                    req = json.loads(line.decode("utf-8"))
                    if not isinstance(req, dict):
                        raise ValueError("se esperaba un objeto JSON")
                    resp = srv.handle_command(req)
                except (ValueError, SettingsError) as e:
                    resp = {"ok": False, "error": str(e)}
                except Z.ASIError as e:
                    resp = {"ok": False, "error": str(e)}
                if isinstance(req, dict) and "id" in req:
                    resp["id"] = req["id"]
                self.wfile.write((json.dumps(resp) + "\n").encode("utf-8"))
                self.wfile.flush()
        except OSError:
            pass
        log.info("cliente de control %s desconectado", peer)


class ThreadedTCP(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


class App:
    def __init__(self, args):
        self.args = args
        self.sdk = Z.SDK(args.sdk)
        log.info("asicam %s, SDK ZWO %s (%s)", VERSION, self.sdk.version, args.sdk)
        self.settings = Settings(args.config)
        self.capturer = Capturer(self.sdk, self.settings, args.camera, args.rt)
        self.stop_event = threading.Event()
        self.threads = []

    # comandos JSON
    def handle_command(self, req):
        cmd = req.get("cmd")
        cap = self.capturer
        if cmd == "hello":
            return {"ok": True, "server": "asicam", "version": VERSION, "sdk": self.sdk.version,
                    "ports": {"control": self.args.ctrl_port, "data": self.args.data_port},
                    "frame_header": {"size": FRAME_HDR.size, "version": FRAME_VERSION,
                                     "format": str(FRAME_HDR.format)}}
        if cmd == "info":
            if cap.camera_info is None:
                return {"ok": False, "error": "no hay camara detectada"}
            controls = {}
            for key, (ctl, auto_key) in CONTROL_KEYS.items():
                c = (cap.controls or {}).get(ctl)
                if c:
                    controls[key] = {"name": c["name"], "min": c["min"], "max": c["max"],
                                     "default": c["default"], "auto": c["auto"], "writable": c["writable"],
                                     "auto_key": auto_key if c["auto"] else None,
                                     "hidden": c.get("hidden", False)}
            return {"ok": True, "camera": cap.camera_info, "controls": controls}
        if cmd == "get":
            s, _, _ = self.settings.snapshot()
            return {"ok": True, "settings": s}
        if cmd == "set":
            changes = self.settings.validate(req.get("settings"), cap.camera_info, cap.controls)
            s = self.settings.apply(changes)
            log.info("ajustes: %s", json.dumps(changes, sort_keys=True))
            return {"ok": True, "settings": s}
        if cmd == "status":
            st = cap.status()
            st["ok"] = True
            st["sdk"] = self.sdk.version
            st["version"] = VERSION
            return st
        raise ValueError("comando desconocido: %r (hello, info, get, set, status)" % cmd)

    # servidores
    def _data_server(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.args.bind, self.args.data_port))
        srv.listen(4)
        srv.settimeout(0.5)
        while not self.stop_event.is_set():
            try:
                sock, addr = srv.accept()
            except socket.timeout:
                continue
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.settimeout(15)
            log.info("cliente de datos %s conectado", addr[0])
            client = FrameClient(sock, addr, self.capturer.remove_client)
            self.capturer.add_client(client)
        srv.close()
        with self.capturer.clients_lock:
            clients = list(self.capturer.clients)
        for c in clients:
            c.close()

    def _discovery_server(self):
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        u.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        u.bind((self.args.bind, self.args.ctrl_port))
        u.settimeout(0.5)
        while not self.stop_event.is_set():
            try:
                data, addr = u.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                continue
            if data.strip() != DISCOVERY_MSG:
                continue
            info = self.capturer.camera_info
            reply = {"server": "asicam", "version": VERSION, "host": socket.gethostname(),
                     "camera": info["name"] if info else None,
                     "control": self.args.ctrl_port, "data": self.args.data_port,
                     "state": self.capturer.state}
            try:
                u.sendto(b"ASICAM " + json.dumps(reply).encode("utf-8"), addr)
            except OSError:
                pass
        u.close()

    def run(self):
        self.capturer.probe()
        ctrl = ThreadedTCP((self.args.bind, self.args.ctrl_port), ControlHandler)
        ctrl.app = self
        for target, name in ((ctrl.serve_forever, "control"), (self._data_server, "datos"),
                             (self._discovery_server, "busqueda")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self.threads.append(t)
        self.capturer.start()
        log.info("escuchando: control %d/tcp, datos %d/tcp, busqueda %d/udp",
                 self.args.ctrl_port, self.args.data_port, self.args.ctrl_port)

        def _stop(signum, _frame):
            log.info("senal %d: cerrando", signum)
            self.stop_event.set()
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
        while not self.stop_event.is_set():
            self.stop_event.wait(0.5)
        ctrl.shutdown()
        ctrl.server_close()
        self.capturer.stop_event.set()
        self.capturer.join(10)
        for t in self.threads:
            t.join(3)
        log.info("listo")


def default_sdk():
    env = os.environ.get("ASICAM_SDK")
    if env:
        return env
    for p in SDK_CANDIDATES:
        if "/" not in p or os.path.exists(p):
            return p
    return SDK_CANDIDATES[-1]


def main(argv=None):
    p = argparse.ArgumentParser(description="Relay de video para camaras ZWO ASI")
    p.add_argument("--sdk", default=default_sdk(), help="ruta a libASICamera2 (o variable ASICAM_SDK)")
    p.add_argument("--camera", type=int, default=0, help="indice de la camara (0 = la primera)")
    p.add_argument("--bind", default="0.0.0.0", help="direccion donde escuchar")
    p.add_argument("--ctrl-port", type=int, default=CTRL_PORT)
    p.add_argument("--data-port", type=int, default=DATA_PORT)
    p.add_argument("--config", default=os.path.expanduser("~/.config/asicam/settings.json"),
                   help="archivo JSON donde se guardan los ajustes")
    p.add_argument("--rt", action="store_true", help="captura con prioridad de tiempo real (requiere root)")
    p.add_argument("--debug", action="store_true")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    App(args).run()


if __name__ == "__main__":
    main()
