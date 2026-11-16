#!/usr/bin/env python3
"""Control PTZ de la montura ufomount desde la Raspberry (reemplaza al Arduino).

Es el firmware v4.1 (firmware/ptztest/ptztest.ino) pasado a Python, con el
mismo protocolo de lineas, asi scripts/ptz_gui.py lo maneja sin cambios:

    5000/TCP  comandos de texto terminados en \\n (una conexion a la vez;
              una nueva reemplaza a la anterior)
    5000/UDP  busqueda: a "UFOMOUNT?" contesta "UFOMOUNT PTZ v5.0 ip=<ip>:5000 ..."

Comandos (mayus/minus indistinto), iguales que en el firmware:
    PING -> PONG PTZ        X <us> / Y <us>       SX / SY / STOP
    ATTACH / DETACH (y ATTACHX, ATTACHY, DETACHX, DETACHY)
    LIMX <min> <max> / LIMY <min> <max>          RATE / RATEX / RATEY <us_s>
    IDLE <ms>   WDT <ms>   STATUS   NET

Los pulsos los genera el PWM por hardware de la Pi, no Linux: no tiemblan
aunque la CPU este ocupada con la camara. Canal 0 = GPIO18 (pin 12) = X,
canal 1 = GPIO13 (pin 33) = Y. Requiere en /boot/config.txt:
    dtoverlay=pwm-2chan,pin=18,func=2,pin2=13,func2=4
y que el usuario este en el grupo gpio (o root).

Normalmente lo arranca asicam_server.py junto con la camara. Tambien solo:
    python3 ptz.py               # PWM real
    python3 ptz.py --simulate    # sin servos (para probar el protocolo)

Compatible con Python 3.7.
"""

import argparse
import logging
import os
import re
import signal
import socket
import threading
import time

VERSION = "v5.0"
TCP_PORT = 5000
DISCOVERY_PORT = 5000
DISCOVERY_MSG = b"UFOMOUNT?"

PWM_CHIP = "/sys/class/pwm/pwmchip0"
PWM_PERIOD_NS = 20000000          # 20 ms = 50 Hz, como la libreria Servo
CHANNEL_X, CHANNEL_Y = 0, 1       # GPIO18 / GPIO13 con el overlay de arriba

CENTER_US = 1500
HARD_MIN_US = 500                 # rango completo confirmado del servo
HARD_MAX_US = 2500
DEFAULT_LIM_X = (500, 2500)
DEFAULT_LIM_Y = (944, 1833)       # tilt -50 (suelo) a +30 (cielo): mas alla topa
DEFAULT_RATE = 500                # us/s
DEFAULT_IDLE_MS = 3000            # auto-detach tras inactividad (0 = nunca)
TICK_S = 0.020                    # el firmware mueve cada 20 ms
MAX_TICK_MS = 60.0                # si un tick se atrasa no se "recupera" de golpe
ATTACH_STAGGER_S = 0.3            # escalona el pico de energizado entre servos
LINE_MAX = 39                     # como el buffer de 40 bytes del firmware
SEND_TIMEOUT_S = 2.0

log = logging.getLogger("asicam.ptz")

_U16 = re.compile(r"^ *(\d+) *$")
_2U16 = re.compile(r"^ *(\d+) +(\d+) *$")


def parse_u16(s):
    """Numero decimal estricto (digitos con espacios alrededor), 0..60000, o None."""
    m = _U16.match(s)
    if not m or len(m.group(1)) > 5 or int(m.group(1)) > 60000:
        return None
    return int(m.group(1))


def parse_2u16(s):
    m = _2U16.match(s)
    if not m:
        return None
    a, b = parse_u16(m.group(1)), parse_u16(m.group(2))
    return None if a is None or b is None else (a, b)


def constrain(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


# --------------------------------------------------------------------- salidas
class SysfsPwm:
    """Un canal del PWM por hardware via /sys/class/pwm."""

    def __init__(self, chip, channel):
        self.name = "pwm%d" % channel
        self.dir = os.path.join(chip, self.name)
        if not os.path.isdir(chip):
            raise OSError("no existe %s (falta dtoverlay=pwm-2chan en /boot/config.txt)" % chip)
        if not os.path.isdir(self.dir):
            self._write(os.path.join(chip, "export"), channel)
            for _ in range(100):
                if os.path.isdir(self.dir):
                    break
                time.sleep(0.05)
        # el periodo primero: con period=0 el kernel rechaza tocar enable
        self._write(self._f("period"), PWM_PERIOD_NS)
        self.disable()

    def _f(self, name):
        return os.path.join(self.dir, name)

    @staticmethod
    def _write(path, value):
        for _ in range(100):
            try:
                with open(path, "w") as f:
                    f.write(str(value))
                return
            except PermissionError:
                time.sleep(0.05)   # recien exportado: udev tarda en dar permisos al grupo gpio
        raise PermissionError("sin permiso para escribir %s (usuario en el grupo gpio?)" % path)

    def set_us(self, us):
        self._write(self._f("duty_cycle"), int(round(us * 1000)))

    def enable(self):
        self._write(self._f("enable"), 1)

    def disable(self):
        self._write(self._f("enable"), 0)


class FakePwm:
    """Salida simulada: guarda el ultimo pulso y si esta activa."""

    def __init__(self, name):
        self.name = name
        self.us = None
        self.on = False

    def set_us(self, us):
        self.us = us

    def enable(self):
        self.on = True

    def disable(self):
        self.on = False


# ------------------------------------------------------------------ la logica
class Axis:
    def __init__(self, lim):
        self.cur = CENTER_US      # pulso aplicado (o estimado si esta suelto)
        self.tgt = CENTER_US      # pulso objetivo
        self.lo, self.hi = lim
        self.rate = DEFAULT_RATE  # us/s
        self.acc = 0.0            # resto fraccional en mili-us
        self.att = False
        self.last_act = 0.0


class Ptz:
    """Estado y comandos de la montura; no sabe nada de la red.

    `src` identifica el canal de cada comando (una conexion TCP, el canal
    JSON...): el watchdog vigila al canal que ordeno el ultimo movimiento.
    """

    def __init__(self, out_x, out_y, clock=time.monotonic):
        self.lock = threading.RLock()
        self.out = {"X": out_x, "Y": out_y}
        self.ax = {"X": Axis(DEFAULT_LIM_X), "Y": Axis(DEFAULT_LIM_Y)}
        self.idle_ms = DEFAULT_IDLE_MS
        self.wdt_ms = 0
        self.last_cmd = {}
        self.move_src = None
        self.clock = clock
        self.net_status = None    # funcion(src) -> linea NET; la pone el servicio
        self._last_tick = None

    def _now(self):
        return self.clock() * 1000.0

    # acoplar en la posicion actual estimada: sin salto al energizar
    def _attach(self, name):
        a = self.ax[name]
        self.out[name].set_us(a.cur)
        self.out[name].enable()
        a.att = True
        a.last_act = self._now()

    def _detach(self, name):
        a = self.ax[name]
        a.tgt = a.cur
        self.out[name].disable()
        a.att = False

    def is_moving(self):
        return any(a.att and a.cur != a.tgt for a in self.ax.values())

    def _stop_all(self):
        for a in self.ax.values():
            a.tgt = a.cur

    def touch(self, src):
        """Una linea recibida por el canal `src` (lo mantiene vivo para el watchdog)."""
        with self.lock:
            self.last_cmd[src] = self._now()

    def status_line(self):
        x, y = self.ax["X"], self.ax["Y"]
        return ("attX=%d attY=%d X=%d/%d [%d-%d] Y=%d/%d [%d-%d] rateX=%d rateY=%d idle=%d"
                % (x.att, y.att, x.cur, x.tgt, x.lo, x.hi, y.cur, y.tgt, y.lo, y.hi,
                   x.rate, y.rate, self.idle_ms))

    def state(self):
        """Estado como diccionario (para el canal JSON de asicam)."""
        with self.lock:
            d = {"idle_ms": self.idle_ms, "wdt_ms": self.wdt_ms, "moving": self.is_moving()}
            for name, a in self.ax.items():
                d[name] = {"attached": a.att, "cur": a.cur, "tgt": a.tgt, "min": a.lo,
                           "max": a.hi, "rate": a.rate}
            return d

    def handle(self, line, src):
        """Ejecuta una linea de comando; devuelve la lista de lineas de respuesta."""
        cmd = line.upper()
        if cmd == "ATTACH":
            # fuera del lock durante la pausa: el tick sigue moviendo/vigilando
            with self.lock:
                self._attach("X")
            time.sleep(ATTACH_STAGGER_S)
            with self.lock:
                self._attach("Y")
            return ["OK ATTACH X+Y"]
        with self.lock:
            return self._handle(cmd, src)

    def _handle(self, cmd, src):
        now = self._now()
        if cmd == "PING":
            return ["PONG PTZ"]
        if cmd[:2] in ("X ", "Y "):
            name = cmd[0]
            a = self.ax[name]
            v = parse_u16(cmd[2:])
            if v is None or not HARD_MIN_US <= v <= HARD_MAX_US:
                return ["ERR %s: valor invalido (500-2500)" % name]
            if not a.att:
                self._attach(name)
            a.tgt = constrain(v, a.lo, a.hi)
            a.last_act = now
            self.move_src = src
            return ["OK %s -> %d" % (name, a.tgt)]
        if cmd in ("SX", "SY"):
            a = self.ax[cmd[1]]
            a.tgt = a.cur
            return ["OK %s @%d" % (cmd, a.cur)]
        if cmd == "STOP":
            self._stop_all()
            return ["OK STOP @%d,%d" % (self.ax["X"].cur, self.ax["Y"].cur)]
        if cmd in ("ATTACHX", "ATTACHY"):
            self._attach(cmd[-1])
            return ["OK " + cmd]
        if cmd == "DETACH":
            self._detach("X")
            self._detach("Y")
            return ["OK DETACH X+Y"]
        if cmd in ("DETACHX", "DETACHY"):
            self._detach(cmd[-1])
            return ["OK " + cmd]
        if cmd[:5] in ("LIMX ", "LIMY "):
            p = parse_2u16(cmd[5:])
            if p is None or not (p[0] < p[1] and p[0] >= HARD_MIN_US and p[1] <= HARD_MAX_US):
                return ["ERR uso: LIMX <min> <max> dentro de 500-2500"]
            a = self.ax[cmd[3]]
            a.lo, a.hi = p
            a.tgt = constrain(a.tgt, a.lo, a.hi)
            return ["OK LIM %d %d" % p]
        for prefix, names in (("RATEX ", "X"), ("RATEY ", "Y"), ("RATE ", "XY")):
            if cmd.startswith(prefix):
                v = parse_u16(cmd[len(prefix):])
                if v is None:
                    return ["ERR %s: valor invalido" % prefix.strip()]
                for name in names:
                    self.ax[name].rate = constrain(v, 5, 5000)
                return ["OK %s %d" % (prefix.strip(), self.ax[names[0]].rate)]
        if cmd.startswith("IDLE "):
            v = parse_u16(cmd[5:])
            if v is None:
                return ["ERR IDLE: valor invalido"]
            self.idle_ms = v
            return ["OK IDLE %d" % v]
        if cmd.startswith("WDT "):
            v = parse_u16(cmd[4:])
            if v is None:
                return ["ERR WDT: valor invalido"]
            self.wdt_ms = v
            return ["OK WDT %d" % v]
        if cmd == "STATUS":
            return [self.status_line()]
        if cmd == "NET":
            return [self.net_status(src) if self.net_status else "NET hw=raspberry (sin red)"]
        return ["ERR comando desconocido"]

    def tick(self):
        """Avanza el movimiento; llamar cada ~20 ms. Devuelve avisos asincronos."""
        notes = []
        with self.lock:
            now = self._now()
            dt = 0.0 if self._last_tick is None else min(now - self._last_tick, MAX_TICK_MS)
            self._last_tick = now
            for name, a in self.ax.items():
                if a.att and a.cur != a.tgt:
                    a.acc += a.rate * dt           # mili-us
                    step = int(a.acc // 1000)
                    if step:
                        a.acc -= step * 1000
                        if a.cur < a.tgt:
                            a.cur = min(a.cur + step, a.tgt)
                        else:
                            a.cur = max(a.cur - step, a.tgt)
                        self.out[name].set_us(a.cur)
                    a.last_act = now
                else:
                    a.acc = 0.0
            # watchdog de enlace: movimiento en curso y su canal callado -> parar
            if (self.wdt_ms > 0 and self.is_moving()
                    and now - self.last_cmd.get(self.move_src, 0.0) >= self.wdt_ms):
                self._stop_all()
                notes.append("WATCHDOG STOP")
            # auto-detach por inactividad: silencio y cero zumbido en reposo
            if self.idle_ms > 0:
                for name, a in self.ax.items():
                    if a.att and a.cur == a.tgt and now - a.last_act >= self.idle_ms:
                        self._detach(name)
                        notes.append("AUTO DETACH" + name)
        return notes

    def channel_lost(self, src):
        """Se cayo el canal `src`: si era el que movia la montura, se detiene."""
        with self.lock:
            self.last_cmd.pop(src, None)
            if self.is_moving() and self.move_src == src:
                self._stop_all()
                log.info("cliente caido con movimiento -> STOP")
                return True
        return False

    def shutdown(self):
        with self.lock:
            for name in ("X", "Y"):
                try:
                    self._detach(name)
                except OSError as e:
                    log.warning("no pude soltar %s: %s", name, e)


# ------------------------------------------------------------------- la red
class LineBuf:
    """Junta bytes hasta \\n o \\r, como el firmware (ignora bytes de control)."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        lines = []
        for c in data:
            if c in (10, 13):
                if self.buf:
                    lines.append(self.buf.decode("ascii"))
                    self.buf = bytearray()
            elif c < 0x20 or c >= 0x7F:
                pass                      # p. ej. negociacion telnet
            elif len(self.buf) < LINE_MAX:
                self.buf.append(c)
            else:
                self.buf = bytearray()    # linea demasiado larga: descartar
        return lines


class Client:
    _ids = 0

    def __init__(self, sock, addr):
        Client._ids += 1
        self.src = "tcp%d" % Client._ids
        self.sock = sock
        self.addr = addr
        self.send_lock = threading.Lock()
        self.closed = False

    def send(self, line):
        with self.send_lock:
            if self.closed:
                return False
            try:
                self.sock.sendall((line + "\r\n").encode("ascii", "replace"))
                return True
            except OSError:
                return False

    def close(self):
        with self.send_lock:
            if self.closed:
                return
            self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def local_ip_for(remote_ip):
    """IP propia que usa la Pi para llegar a `remote_ip`."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((remote_ip, 9))
        return s.getsockname()[0]
    except OSError:
        return "0.0.0.0"
    finally:
        s.close()


def link_info(ifname="eth0"):
    base = "/sys/class/net/%s/" % ifname
    try:
        with open(base + "operstate") as f:
            up = f.read().strip() == "up"
        with open(base + "speed") as f:
            speed = f.read().strip()
        return ("on" if up else "off"), "%s %s Mbps" % (ifname, speed)
    except OSError:
        return "?", ifname


class PtzService:
    """Servidor TCP de comandos + busqueda UDP + tick de movimiento."""

    def __init__(self, ptz, bind="0.0.0.0", port=TCP_PORT, discovery_port=DISCOVERY_PORT):
        self.ptz = ptz
        self.bind, self.port, self.discovery_port = bind, port, discovery_port
        self.stop_event = threading.Event()
        self.client = None
        self.client_lock = threading.Lock()
        self.threads = []
        self.tcp = self.udp = None
        ptz.net_status = self._net_status

    def start(self):
        """Abre los puertos (lanza OSError si estan ocupados) y arranca los hilos."""
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            if os.name != "nt":   # en Windows SO_REUSEADDR deja robar un puerto en uso
                tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            tcp.bind((self.bind, self.port))
            tcp.listen(2)
            tcp.settimeout(0.5)
            udp.bind((self.bind, self.discovery_port))
            udp.settimeout(0.5)
        except OSError:
            tcp.close()
            udp.close()
            raise
        self.tcp, self.udp = tcp, udp
        for target, name in ((self._tick_loop, "ptz-tick"), (self._accept_loop, "ptz-tcp"),
                             (self._discovery_loop, "ptz-busqueda")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self.threads.append(t)
        log.info("PTZ %s: comandos %d/tcp, busqueda %d/udp", VERSION, self.port, self.discovery_port)

    def stop(self):
        self.stop_event.set()
        with self.client_lock:
            c, self.client = self.client, None
        if c:
            c.close()
        for t in self.threads:
            t.join(2)
        for s in (self.tcp, self.udp):
            if s:
                s.close()
        self.ptz.shutdown()
        log.info("PTZ detenido, servos sueltos")

    def notify(self, line):
        with self.client_lock:
            c = self.client
        log.info("aviso: %s", line)
        if c:
            c.send(line)

    def _net_status(self, src):
        with self.client_lock:
            c = self.client
        ip = c.sock.getsockname()[0] if c and c.src == src else local_ip_for("8.8.8.8")
        link, desc = link_info()
        return "NET hw=raspberry link=%s ip=%s:%d (%s) cliente=%d" % (link, ip, self.port, desc,
                                                                     1 if c else 0)

    def _tick_loop(self):
        nxt = time.monotonic()
        while not self.stop_event.is_set():
            try:
                for note in self.ptz.tick():
                    self.notify(note)
            except OSError as e:
                log.error("PWM: %s", e)
            nxt += TICK_S
            delay = nxt - time.monotonic()
            if delay < -0.5:
                nxt = time.monotonic()   # atrasado de verdad: no encadenar ticks
            elif delay > 0:
                time.sleep(delay)

    def _accept_loop(self):
        while not self.stop_event.is_set():
            try:
                sock, addr = self.tcp.accept()
            except socket.timeout:
                continue
            except OSError:
                if self.stop_event.is_set():
                    break
                continue
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            sock.settimeout(SEND_TIMEOUT_S)
            client = Client(sock, addr)
            with self.client_lock:
                old, self.client = self.client, client
            if old:
                old.send("BYE otra conexion tomo el control")
                old.close()
                self.ptz.channel_lost(old.src)
            self.ptz.touch(client.src)
            client.send("UFOMOUNT PTZ %s LISTO (TCP)" % VERSION)
            log.info("PTZ: cliente %s conectado", addr[0])
            t = threading.Thread(target=self._client_loop, args=(client,), name="ptz-cliente",
                                 daemon=True)
            t.start()

    def _client_loop(self, client):
        lb = LineBuf()
        client.sock.settimeout(None)   # lectura bloqueante; los envios usan sendall
        try:
            while not self.stop_event.is_set():
                try:
                    data = client.sock.recv(256)
                except OSError:
                    break
                if not data:
                    break
                for line in lb.feed(data):
                    self.ptz.touch(client.src)
                    try:
                        replies = self.ptz.handle(line, client.src)
                    except OSError as e:
                        log.error("PWM: %s", e)
                        replies = ["ERR PWM: %s" % e]
                    for r in replies:
                        client.send(r)
        finally:
            client.close()
            with self.client_lock:
                if self.client is client:
                    self.client = None
            self.ptz.channel_lost(client.src)
            log.info("PTZ: cliente %s desconectado", client.addr[0])

    def _discovery_loop(self):
        while not self.stop_event.is_set():
            try:
                data, addr = self.udp.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                if self.stop_event.is_set():
                    break
                continue
            if data[:len(DISCOVERY_MSG)] != DISCOVERY_MSG:
                continue
            ip = self.bind if self.bind not in ("", "0.0.0.0") else local_ip_for(addr[0])
            with self.client_lock:
                busy = 1 if self.client else 0
            reply = "UFOMOUNT PTZ %s ip=%s:%d raspberry cliente=%d" % (VERSION, ip, self.port, busy)
            try:
                self.udp.sendto(reply.encode("ascii"), addr)
            except OSError:
                pass


def make_ptz(simulate=False, chip=PWM_CHIP):
    if simulate:
        return Ptz(FakePwm("X"), FakePwm("Y"))
    return Ptz(SysfsPwm(chip, CHANNEL_X), SysfsPwm(chip, CHANNEL_Y))


def main(argv=None):
    p = argparse.ArgumentParser(description="Control PTZ ufomount en la Raspberry")
    p.add_argument("--bind", default="0.0.0.0")
    p.add_argument("--port", type=int, default=TCP_PORT)
    p.add_argument("--chip", default=PWM_CHIP)
    p.add_argument("--simulate", action="store_true", help="sin servos: PWM simulado")
    p.add_argument("--debug", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    svc = PtzService(make_ptz(a.simulate, a.chip), a.bind, a.port, a.port)
    svc.start()
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    while not stop.is_set():
        stop.wait(0.5)
    svc.stop()


if __name__ == "__main__":
    main()
