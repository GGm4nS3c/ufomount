#!/usr/bin/env python3
"""Cliente de asicam: recibe en el PC los cuadros crudos que reenvia la Raspberry.

Uso como libreria:

    from asicam_client import AsiCam, discover

    print(discover())                         # servidores en la red
    with AsiCam("192.168.200.159") as cam:
        print(cam.info()["camera"]["name"])
        cam.set(exposure_us=27000, gain=0)    # en vivo
        cam.start()                           # empieza a recibir cuadros
        f = cam.get_frame(timeout=2)          # el siguiente cuadro
        print(f.seq, f.image.shape, f.image.dtype, f.exposure_us)
        img12 = f.pixels()                    # RAW16 -> valores 0..4095

Uso por linea de comandos:

    python asicam_client.py find
    python asicam_client.py [--host IP] info | status | get
    python asicam_client.py [--host IP] set exposure_us=27000 gain=0 auto_exposure=false
    python asicam_client.py [--host IP] stream 10
    python asicam_client.py [--host IP] snap foto.fits      (.fits, .pgm o .npy)

Requiere numpy. Protocolo en asicam/README.md.
"""

import argparse
import json
import queue
import socket
import struct
import sys
import threading
import time

import numpy as np

CTRL_PORT = 5300
DATA_PORT = 5301
FRAME_HDR = struct.Struct("<4sHHIdIHHHBBIHHh2xI")
FRAME_MAGIC = b"ASIF"
IMG_TYPES = {0: "RAW8", 2: "RAW16"}
DTYPES = {0: np.uint8, 2: np.dtype("<u2")}
FLAG_AUTO_EXPOSURE = 1
FLAG_AUTO_GAIN = 2

__all__ = ["AsiCam", "AsiCamError", "Frame", "discover", "save_fits", "save_pgm"]


class AsiCamError(Exception):
    """Error devuelto por el servidor o de comunicacion."""


class Frame:
    """Un cuadro recibido. `image` es un arreglo numpy (alto, ancho)."""

    __slots__ = ("seq", "timestamp", "received", "exposure_us", "gain", "width", "height", "bin",
                 "image_type", "flags", "dropped", "restarts", "temperature_c", "image", "bit_depth")

    def __init__(self, fields, image, received, bit_depth):
        (_magic, _ver, _hsize, self.seq, self.timestamp, self.exposure_us, self.gain, self.width,
         self.height, self.bin, itype, self.dropped, self.restarts, self.flags, temp10, _size) = fields
        self.image_type = IMG_TYPES.get(itype, str(itype))
        self.temperature_c = None if temp10 == -32768 else temp10 / 10.0
        self.image = image
        self.received = received
        self.bit_depth = bit_depth

    @property
    def auto_exposure(self):
        return bool(self.flags & FLAG_AUTO_EXPOSURE)

    @property
    def auto_gain(self):
        return bool(self.flags & FLAG_AUTO_GAIN)

    @property
    def latency(self):
        """Segundos entre la captura en la Pi y la llegada al PC (depende de que ambos relojes esten en hora)."""
        return self.received - self.timestamp

    def pixels(self):
        """Valores reales del sensor: en RAW16 corre los bits utiles abajo (0..2^bits-1)."""
        if self.image_type == "RAW16" and self.bit_depth and self.bit_depth < 16:
            return self.image >> (16 - self.bit_depth)
        return self.image

    def __repr__(self):
        return "<Frame #%d %dx%d %s exp=%dus gain=%d>" % (self.seq, self.width, self.height,
                                                         self.image_type, self.exposure_us, self.gain)


def _local_broadcasts():
    out = {"255.255.255.255"}
    try:
        for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = ai[4][0]
            if not ip.startswith("127."):
                out.add(ip.rsplit(".", 1)[0] + ".255")
    except OSError:
        pass
    return out


def discover(timeout=1.5, targets=(), port=CTRL_PORT):
    """Busca servidores asicam. Devuelve una lista de dicts (con la clave 'ip')."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.2)
    for d in list(_local_broadcasts()) + list(targets):
        try:
            s.sendto(b"ASICAM?", (d, port))
        except OSError:
            pass
    found = {}
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            data, addr = s.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if data.startswith(b"ASICAM "):
            try:
                info = json.loads(data[7:].decode("utf-8"))
            except ValueError:
                continue
            info["ip"] = addr[0]
            # una misma Pi puede responder desde dos IPs: se agrupa por nombre de equipo
            entry = found.setdefault(info.get("host") or addr[0], dict(info, ips=[]))
            if addr[0] not in entry["ips"]:
                entry["ips"].append(addr[0])
    s.close()
    return list(found.values())


class AsiCam:
    """Conexion a un servidor asicam: control (ajustes) y flujo de cuadros."""

    def __init__(self, host, ctrl_port=CTRL_PORT, data_port=DATA_PORT, timeout=10.0,
                 queue_size=4, auto_reconnect=True):
        self.host = host
        self.ctrl_port = ctrl_port
        self.data_port = data_port
        self.timeout = timeout
        self.auto_reconnect = auto_reconnect
        self._ctrl = None
        self._ctrl_file = None
        self._ctrl_lock = threading.Lock()
        self._req_id = 0
        self._queue = queue.Queue(maxsize=max(1, queue_size))
        self._latest = None
        self._callbacks = []
        self._rx_thread = None
        self._rx_sock = None
        self._running = False
        self._stats_lock = threading.Lock()
        self._stats = {}
        self._bit_depth = None
        self._reset_stats()
        self._connect_ctrl()
        hello = self.request("hello")
        if hello.get("frame_header", {}).get("size", FRAME_HDR.size) < FRAME_HDR.size:
            raise AsiCamError("el servidor usa una cabecera de cuadro distinta: %r" % hello.get("frame_header"))
        self.server = hello

    # ---------- control ----------
    def _connect_ctrl(self):
        self._close_ctrl()
        try:
            self._ctrl = socket.create_connection((self.host, self.ctrl_port), timeout=self.timeout)
        except OSError as e:
            raise AsiCamError("no pude conectar con %s:%d (%s)" % (self.host, self.ctrl_port, e))
        self._ctrl_file = self._ctrl.makefile("rwb")

    def _close_ctrl(self):
        for x in (self._ctrl_file, self._ctrl):
            if x is not None:
                try:
                    x.close()
                except OSError:
                    pass
        self._ctrl = self._ctrl_file = None

    def request(self, cmd, **params):
        """Manda un comando y devuelve la respuesta (dict). Lanza AsiCamError si ok=false."""
        with self._ctrl_lock:
            for attempt in (1, 2):
                try:
                    if self._ctrl is None:
                        self._connect_ctrl()
                    self._req_id += 1
                    req = dict(params, cmd=cmd, id=self._req_id)
                    self._ctrl_file.write((json.dumps(req) + "\n").encode("utf-8"))
                    self._ctrl_file.flush()
                    line = self._ctrl_file.readline()
                    if not line:
                        raise OSError("el servidor cerro la conexion")
                    resp = json.loads(line.decode("utf-8"))
                    break
                except (OSError, ValueError) as e:
                    self._close_ctrl()
                    if attempt == 2 or not self.auto_reconnect:
                        raise AsiCamError("sin respuesta de %s (%s)" % (self.host, e))
                    time.sleep(0.5)
        if not resp.get("ok"):
            raise AsiCamError(resp.get("error", "error desconocido"))
        return resp

    def info(self):
        r = self.request("info")
        self._bit_depth = r.get("camera", {}).get("bit_depth")
        return r

    def settings(self):
        return self.request("get")["settings"]

    def set(self, **settings):
        """Cambia ajustes (ver README). Devuelve todos los ajustes ya aplicados."""
        return self.request("set", settings=settings)["settings"]

    def status(self):
        return self.request("status")

    # ---------- flujo de cuadros ----------
    def on_frame(self, callback):
        """callback(frame) se llama en el hilo receptor por cada cuadro: debe ser rapido."""
        self._callbacks.append(callback)

    def start(self):
        """Conecta el canal de datos y empieza a recibir en segundo plano."""
        if self._running:
            return
        if self._bit_depth is None:
            try:
                self.info()
            except AsiCamError:
                pass
        self._running = True
        self._reset_stats()
        self._rx_thread = threading.Thread(target=self._rx_loop, name="asicam-rx", daemon=True)
        self._rx_thread.start()

    def stop(self):
        self._running = False
        s = self._rx_sock
        if s is not None:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._rx_thread is not None:
            self._rx_thread.join(5)
            self._rx_thread = None

    def get_frame(self, timeout=None):
        """El siguiente cuadro en orden (o None si no llega a tiempo).

        Si no se piden cuadros a tiempo, la cola guarda solo los mas recientes y los
        viejos se cuentan en stats()['dropped_local'].
        """
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def latest(self):
        """El cuadro mas reciente recibido (sin esperar), o None."""
        return self._latest

    def stats(self):
        with self._stats_lock:
            st = dict(self._stats)
            times = list(st.pop("_times"))
        st["fps"] = round((len(times) - 1) / (times[-1] - times[0]), 2) if len(times) > 1 and times[-1] > times[0] else 0.0
        st["connected"] = self._rx_sock is not None
        return st

    def _reset_stats(self):
        with self._stats_lock:
            self._stats = {"received": 0, "gaps": 0, "dropped_local": 0, "reconnects": 0,
                           "bytes": 0, "last_seq": None, "_times": []}

    def _recv_exact(self, sock, view):
        pos, n = 0, len(view)
        while pos < n:
            k = sock.recv_into(view[pos:], n - pos)
            if k == 0:
                raise OSError("el servidor cerro el canal de datos")
            pos += k

    def _rx_loop(self):
        hdr = bytearray(FRAME_HDR.size)
        hview = memoryview(hdr)
        first = True
        while self._running:
            try:
                sock = socket.create_connection((self.host, self.data_port), timeout=self.timeout)
            except OSError:
                if not self.auto_reconnect:
                    self._running = False
                    break
                time.sleep(1.0)
                continue
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
            sock.settimeout(max(self.timeout, 30.0))
            self._rx_sock = sock
            if not first:
                with self._stats_lock:
                    self._stats["reconnects"] += 1
                    self._stats["last_seq"] = None  # el servidor pudo reiniciar la numeracion
            first = False
            try:
                while self._running:
                    self._recv_exact(sock, hview)
                    fields = FRAME_HDR.unpack(hdr)
                    if fields[0] != FRAME_MAGIC:
                        raise OSError("cabecera invalida (%r)" % bytes(fields[0]))
                    extra = fields[2] - FRAME_HDR.size
                    if extra > 0:  # cabeceras futuras mas largas: se saltan los bytes nuevos
                        self._recv_exact(sock, memoryview(bytearray(extra)))
                    size = fields[-1]
                    buf = bytearray(size)
                    self._recv_exact(sock, memoryview(buf))
                    now = time.time()
                    dtype = DTYPES.get(fields[10], np.uint8)
                    w, h = fields[7], fields[8]
                    img = np.frombuffer(buf, dtype=dtype)
                    if img.size == w * h:
                        img = img.reshape(h, w)
                    frame = Frame(fields, img, now, self._bit_depth)
                    self._deliver(frame, size)
            except (OSError, ValueError):
                pass
            finally:
                self._rx_sock = None
                try:
                    sock.close()
                except OSError:
                    pass
            if not self.auto_reconnect:
                self._running = False
            elif self._running:
                time.sleep(0.5)

    def _deliver(self, frame, size):
        with self._stats_lock:
            st = self._stats
            last = st["last_seq"]
            if last is not None and frame.seq > last + 1:
                st["gaps"] += frame.seq - last - 1
            st["last_seq"] = frame.seq
            st["received"] += 1
            st["bytes"] += size
            st["_times"].append(time.monotonic())
            if len(st["_times"]) > 60:
                del st["_times"][:30]
        self._latest = frame
        while True:
            try:
                self._queue.put_nowait(frame)
                break
            except queue.Full:
                try:
                    self._queue.get_nowait()
                    with self._stats_lock:
                        self._stats["dropped_local"] += 1
                except queue.Empty:
                    pass
        for cb in list(self._callbacks):
            try:
                cb(frame)
            except Exception as e:  # noqa: BLE001 - un callback roto no debe cortar el flujo
                print("asicam: error en callback: %r" % e, file=sys.stderr)

    # ---------- cierre ----------
    def close(self):
        self.stop()
        with self._ctrl_lock:
            self._close_ctrl()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ---------- guardar cuadros ----------

def save_pgm(path, frame):
    """PGM binario (8 o 16 bits), lo abre casi cualquier visor."""
    img = frame.image
    maxval = 255 if img.dtype == np.uint8 else 65535
    with open(path, "wb") as f:
        f.write(b"P5\n%d %d\n%d\n" % (img.shape[1], img.shape[0], maxval))
        f.write(img.astype(">u2").tobytes() if maxval == 65535 else img.tobytes())


def save_fits(path, frame, extra=None):
    """FITS simple (el formato habitual en astronomia), con exposicion, ganancia y hora."""
    img = frame.image
    if img.dtype == np.uint8:
        bitpix, data, bzero = 8, img.tobytes(), None
    else:  # FITS no tiene 16 bits sin signo: se guarda con BZERO=32768
        bitpix, bzero = 16, 32768
        data = (img.astype(np.int32) - 32768).astype(">i2").tobytes()
    t = time.gmtime(frame.timestamp)
    date_obs = time.strftime("%Y-%m-%dT%H:%M:%S", t) + ("%.3f" % (frame.timestamp % 1))[1:]
    cards = [("SIMPLE", True, "conforms to FITS standard"), ("BITPIX", bitpix, None), ("NAXIS", 2, None),
             ("NAXIS1", img.shape[1], None), ("NAXIS2", img.shape[0], None)]
    if bzero is not None:
        cards += [("BZERO", bzero, None), ("BSCALE", 1, None)]
    cards += [("EXPTIME", frame.exposure_us / 1e6, "exposicion [s]"), ("GAIN", frame.gain, None),
              ("XBINNING", frame.bin, None), ("YBINNING", frame.bin, None),
              ("DATE-OBS", date_obs, "hora UTC de llegada a la Pi"), ("FRAMESEQ", frame.seq, None)]
    if frame.temperature_c is not None:
        cards.append(("CCD-TEMP", frame.temperature_c, "temperatura del sensor [C]"))
    for k, v in (extra or {}).items():
        cards.append((k[:8].upper(), v, None))

    def card(k, v, c):
        if isinstance(v, bool):
            val = "%20s" % ("T" if v else "F")
        elif isinstance(v, (int, np.integer)):
            val = "%20d" % v
        elif isinstance(v, float):
            val = "%20s" % ("%.10G" % v)
        else:
            val = "'%-8s'" % str(v).replace("'", "''")
        s = "%-8s= %s" % (k, val)
        if c:
            s += " / " + c
        return s[:80].ljust(80)

    header = "".join(card(*c) for c in cards) + "END".ljust(80)
    header += " " * (-len(header) % 2880)
    with open(path, "wb") as f:
        f.write(header.encode("ascii", "replace"))
        f.write(data)
        f.write(b"\0" * (-len(data) % 2880))


# ---------- linea de comandos ----------

def _parse_value(v):
    low = v.lower()
    if low in ("true", "si", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none", "default"):
        return None
    try:
        return int(v)
    except ValueError:
        return v


def _pick_host(host):
    if host:
        return host
    found = discover()
    if not found:
        sys.exit("no encontre servidores asicam en la red (usar --host IP)")
    return found[0]["ip"]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Cliente de asicam (relay de camara ZWO en la Raspberry)")
    ap.add_argument("--host", help="IP del servidor (si no, se busca en la red)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("find", help="busca servidores en la red")
    sub.add_parser("info", help="datos de la camara y rangos de los ajustes")
    sub.add_parser("status", help="estado del servidor")
    sub.add_parser("get", help="ajustes actuales")
    p = sub.add_parser("set", help="cambia ajustes: clave=valor ...")
    p.add_argument("pairs", nargs="+")
    p = sub.add_parser("stream", help="recibe cuadros N segundos y muestra estadisticas")
    p.add_argument("secs", type=float, nargs="?", default=10.0)
    p = sub.add_parser("snap", help="guarda un cuadro (.fits, .pgm o .npy)")
    p.add_argument("path")
    a = ap.parse_args(argv)

    if a.cmd == "find":
        for s in discover():
            print(json.dumps(s, ensure_ascii=False))
        return
    with AsiCam(_pick_host(a.host)) as cam:
        if a.cmd in ("info", "status", "get"):
            r = {"info": cam.info, "status": cam.status, "get": cam.settings}[a.cmd]()
            print(json.dumps(r, indent=1, ensure_ascii=False))
        elif a.cmd == "set":
            changes = {}
            for pair in a.pairs:
                if "=" not in pair:
                    sys.exit("formato: clave=valor (%r)" % pair)
                k, v = pair.split("=", 1)
                changes[k.strip()] = _parse_value(v.strip())
            print(json.dumps(cam.set(**changes), indent=1, ensure_ascii=False))
        elif a.cmd == "stream":
            cam.start()
            end = time.monotonic() + a.secs
            while time.monotonic() < end:
                f = cam.get_frame(timeout=1.0)
                if f is not None and f.seq % 10 == 0:
                    px = f.pixels()
                    print("%r  min=%d max=%d media=%.1f  latencia=%.3f s" % (
                        f, px.min(), px.max(), px.mean(), f.latency))
            print(json.dumps(cam.stats()))
        elif a.cmd == "snap":
            cam.start()
            f = cam.get_frame(timeout=15)
            if f is None:
                sys.exit("no llego ningun cuadro")
            if a.path.lower().endswith(".npy"):
                np.save(a.path, f.image)
            elif a.path.lower().endswith(".pgm"):
                save_pgm(a.path, f)
            else:
                save_fits(a.path, f)
            print("guardado %s: %r" % (a.path, f))


if __name__ == "__main__":
    try:
        main()
    except AsiCamError as e:
        sys.exit("error: %s" % e)
    except KeyboardInterrupt:
        pass
