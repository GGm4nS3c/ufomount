#!/usr/bin/env python3
"""Pruebas del control PTZ del servidor asicam (asicam/pi/ptz.py).

Uso:
    python asicam/tools/check_ptz.py --simulate          # servidor simulado local
    python asicam/tools/check_ptz.py 192.168.200.159     # contra la Pi (MUEVE los servos)

Contra la Pi los movimientos son chicos y lentos alrededor del centro
(X 1450-1600 us, Y 1450-1550 us). Al terminar deja los ajustes por defecto
del firmware (limites, velocidad, IDLE 3000, WDT 0) y los servos sueltos.
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "..", "pi", "asicam_server.py")

# la misma expresion que usa scripts/ptz_gui.py para leer STATUS
STATUS_RE = re.compile(
    r"attX=(\d) attY=(\d) X=(\d+)/(\d+) \[(\d+)-(\d+)\]"
    r" Y=(\d+)/(\d+) \[(\d+)-(\d+)\] rateX=(\d+) rateY=(\d+) idle=(\d+)"
)
REPLY_PREFIXES = ("OK", "ERR", "PONG", "attX=", "NET", "BYE")
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print("  [%s] %s%s" % ("OK " if ok else "FALLA", name, (" - " + detail) if detail else ""), flush=True)
    return ok


class Conn:
    def __init__(self, host, port):
        self.s = socket.create_connection((host, port), timeout=5)
        self.s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.buf = b""
        self.notes = []
        self.eof = False

    def readline(self, timeout=2.0):
        end = time.monotonic() + timeout
        while b"\n" not in self.buf:
            left = end - time.monotonic()
            if left <= 0 or self.eof:
                return None
            self.s.settimeout(left)
            try:
                data = self.s.recv(4096)
            except socket.timeout:
                return None
            except OSError:
                self.eof = True
                return None
            if not data:
                self.eof = True
                return None
            self.buf += data
        line, self.buf = self.buf.split(b"\n", 1)
        return line.decode("ascii").strip()

    def send_raw(self, data):
        self.s.sendall(data)

    def cmd(self, line, timeout=2.0):
        """Envia una linea y devuelve su respuesta; los avisos van a self.notes."""
        self.s.sendall((line + "\n").encode("ascii"))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            r = self.readline(end - time.monotonic())
            if r is None:
                return None
            if r.startswith(REPLY_PREFIXES):
                return r
            self.notes.append(r)
        return None

    def status(self):
        m = STATUS_RE.match(self.cmd("STATUS") or "")
        if not m:
            return None
        v = [int(g) for g in m.groups()]
        return {"attX": v[0], "attY": v[1], "X": v[2], "tX": v[3], "minX": v[4], "maxX": v[5],
                "Y": v[6], "tY": v[7], "minY": v[8], "maxY": v[9], "rateX": v[10], "rateY": v[11],
                "idle": v[12]}

    def wait_note(self, prefix, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for n in self.notes:
                if n.startswith(prefix):
                    self.notes.remove(n)
                    return time.monotonic()
            r = self.readline(min(0.1, max(0.0, end - time.monotonic())))
            if r is not None:
                self.notes.append(r)
        return None

    def wait_still(self, axis, timeout=8.0):
        """Espera a que el eje llegue a su objetivo; devuelve (segundos, valores vistos)."""
        t0 = time.monotonic()
        seen = []
        while time.monotonic() - t0 < timeout:
            st = self.status()
            if st:
                seen.append(st[axis])
                if st[axis] == st["t" + axis]:
                    return time.monotonic() - t0, seen
            time.sleep(0.02)
        return None, seen

    def close(self):
        try:
            self.s.close()
        except OSError:
            pass


def json_cmd(host, port, **req):
    with socket.create_connection((host, port), timeout=5) as s:
        s.sendall((json.dumps(req) + "\n").encode("utf-8"))
        f = s.makefile("rb")
        return json.loads(f.readline().decode("utf-8"))


def run(host, port, ctrl_port):
    print("1) Busqueda UDP")
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    u.settimeout(2)
    u.sendto(b"UFOMOUNT?", (host, port))
    try:
        reply = u.recvfrom(256)[0].decode("ascii")
    except socket.timeout:
        reply = ""
    u.close()
    check("contesta UFOMOUNT?", reply.startswith("UFOMOUNT PTZ") and " ip=" in reply, reply)

    print("2) Conexion y comandos basicos")
    c = Conn(host, port)
    banner = c.readline(3)
    check("saludo al conectar", (banner or "").startswith("UFOMOUNT PTZ") and "LISTO" in banner, banner)
    check("PING", c.cmd("PING") == "PONG PTZ")
    check("minusculas", c.cmd("ping") == "PONG PTZ")
    c.send_raw(b"\x01\xff\x1bPING\r\n")
    r = c.readline(1)
    check("ignora bytes de control (y CR+LF da una sola linea)", r == "PONG PTZ" and c.readline(0.3) is None, repr(r))
    c.send_raw(b"Q" * 60 + b"\n")
    tail = c.readline(1)   # el firmware descarta 39 y lo que sigue es otra linea
    check("linea demasiado larga no rompe nada", tail == "ERR comando desconocido" and c.cmd("PING") == "PONG PTZ",
          repr(tail))
    net = c.cmd("NET")
    check("NET", (net or "").startswith("NET hw="), net)

    print("3) Estado inicial conocido")
    for line, want in (("DETACH", "OK DETACH X+Y"), ("LIMX 500 2500", "OK LIM 500 2500"),
                       ("LIMY 944 1833", "OK LIM 944 1833"), ("RATE 500", "OK RATE 500"),
                       ("IDLE 3000", "OK IDLE 3000"), ("WDT 0", "OK WDT 0")):
        r = c.cmd(line)
        if r != want:
            check(line, False, repr(r))
    st = c.status()
    check("STATUS con el formato de la GUI", st is not None and st["attX"] == 0 and st["attY"] == 0
          and (st["minY"], st["maxY"]) == (944, 1833) and st["rateX"] == 500 and st["idle"] == 3000,
          str(st))

    print("4) Validacion (igual que el firmware)")
    for line, want in (("X 9999", "ERR X: valor invalido (500-2500)"), ("Y abc", "ERR Y: valor invalido (500-2500)"),
                       ("X 1500x", "ERR X: valor invalido (500-2500)"), ("X", "ERR comando desconocido"),
                       ("FOO", "ERR comando desconocido"), ("LIMX 2000 1000", "ERR uso: LIMX <min> <max> dentro de 500-2500"),
                       ("LIMY 400 1500", "ERR uso: LIMX <min> <max> dentro de 500-2500"),
                       ("RATE x", "ERR RATE: valor invalido"), ("RATEX 3", "OK RATEX 5"),
                       ("RATEY 9000", "OK RATEY 5000"), ("IDLE -1", "ERR IDLE: valor invalido"),
                       ("RATE 500", "OK RATE 500")):
        r = c.cmd(line)
        check("%s -> %s" % (line, want), r == want, "" if r == want else repr(r))
    st = c.status()
    check("los errores no movieron ni acoplaron", st and st["attX"] == 0 and st["attY"] == 0)

    print("5) Movimiento")
    check("X 1500 acopla", c.cmd("X 1500") == "OK X -> 1500")
    check("Y 1500 acopla", c.cmd("Y 1500") == "OK Y -> 1500")
    st = c.status()
    check("ambos acoplados en el centro", st and st["attX"] == 1 and st["attY"] == 1
          and st["X"] == 1500 and st["Y"] == 1500, str(st))
    c.cmd("RATEX 200")
    check("X 1600", c.cmd("X 1600") == "OK X -> 1600")
    dt, seen = c.wait_still("X")
    mid = [v for v in seen if 1500 < v < 1600]
    check("se mueve suave a 200 us/s (100 us en ~0,5 s)", dt is not None and 0.35 <= dt <= 0.9 and len(mid) >= 5,
          "%.2f s, %d posiciones intermedias" % (dt or -1, len(mid)))
    c.cmd("LIMY 1450 1550")
    c.cmd("RATEY 1000")
    check("Y fuera de limite se acota", c.cmd("Y 2400") == "OK Y -> 1550")
    dt, _ = c.wait_still("Y")
    st = c.status()
    check("Y llega al limite", st and st["Y"] == 1550, str(st and st["Y"]))
    check("LIMY re-acota el objetivo vigente", c.cmd("LIMY 1450 1520") == "OK LIM 1450 1520"
          and c.wait_still("Y")[0] is not None and c.status()["Y"] == 1520)
    c.cmd("Y 1500")
    c.wait_still("Y")

    print("6) Paradas")
    c.cmd("RATEX 100")
    c.cmd("X 1500")
    time.sleep(0.4)
    r = c.cmd("STOP")
    st1 = c.status()
    time.sleep(0.3)
    st2 = c.status()
    check("STOP detiene a mitad de camino", (r or "").startswith("OK STOP @") and st1 and st2
          and 1500 < st2["X"] < 1600 and st2["X"] == st2["tX"] == st1["X"], "%s / X=%s" % (r, st2 and st2["X"]))
    c.cmd("X 1600")
    time.sleep(0.2)
    r = c.cmd("SX")
    st1 = c.status()
    time.sleep(0.2)
    check("SX detiene X", (r or "").startswith("OK SX @") and c.status()["X"] == st1["X"], r)

    print("7) Watchdog e inactividad")
    check("WDT 300", c.cmd("WDT 300") == "OK WDT 300")
    c.cmd("X 1450")
    t0 = time.monotonic()
    t = c.wait_note("WATCHDOG STOP", 2.0)   # callados: solo leemos
    check("WATCHDOG STOP si el cliente calla en movimiento", t is not None and 0.25 <= t - t0 <= 0.6,
          "%.2f s" % ((t or t0) - t0))
    st = c.status()
    check("el watchdog dejo la montura quieta", st and st["X"] == st["tX"])
    c.cmd("WDT 0")
    check("IDLE 400", c.cmd("IDLE 400") == "OK IDLE 400")
    tx = c.wait_note("AUTO DETACHX", 2.0)
    ty = c.wait_note("AUTO DETACHY", 2.0)
    st = c.status()
    check("AUTO DETACH de ambos ejes en reposo", tx and ty and st and st["attX"] == 0 and st["attY"] == 0)
    c.cmd("IDLE 3000")
    t0 = time.monotonic()
    r = c.cmd("ATTACH")
    dt = time.monotonic() - t0
    st = c.status()
    check("ATTACH escalonado (~0,3 s)", r == "OK ATTACH X+Y" and 0.25 <= dt <= 1.0 and st["attX"] == st["attY"] == 1,
          "%.2f s" % dt)
    check("DETACH", c.cmd("DETACH") == "OK DETACH X+Y" and c.status()["attX"] == 0)

    print("8) Conexiones")
    c2 = Conn(host, port)
    b2 = c2.readline(3)
    bye = c.readline(2)
    c.readline(1)
    check("una conexion nueva reemplaza a la anterior", (b2 or "").startswith("UFOMOUNT PTZ")
          and bye == "BYE otra conexion tomo el control" and c.eof, "%r / %r" % (bye, b2))
    c.close()
    c2.cmd("RATEX 100")
    x0 = c2.status()["X"]
    c2.cmd("X 1450")                        # ~1 s de recorrido a 100 us/s
    time.sleep(0.3)
    c2.close()                              # se cae el cliente que movia
    time.sleep(0.3)
    c3 = Conn(host, port)
    c3.readline(3)
    st1 = c3.status()
    time.sleep(0.3)
    st2 = c3.status()
    check("si se cae el cliente que movia, STOP", st1 and st2 and st1["X"] == st2["X"] == st2["tX"]
          and 1450 < st2["X"] < x0, "X %s -> %s" % (x0, st2 and st2["X"]))

    print("9) Canal JSON de asicam")
    try:
        h = json_cmd(host, ctrl_port, cmd="hello")
        check("hello anuncia el PTZ", isinstance(h.get("ptz"), dict) and h["ptz"].get("port") == port, str(h.get("ptz")))
        r = json_cmd(host, ctrl_port, cmd="ptz", line="PING")
        check("cmd ptz", r.get("ok") and r.get("replies") == ["PONG PTZ"], str(r))
        r = json_cmd(host, ctrl_port, cmd="ptz", line="X 99999")
        check("cmd ptz con error", r.get("ok") is False and r["replies"][0].startswith("ERR"), str(r))
        s = json_cmd(host, ctrl_port, cmd="status")
        check("status incluye el PTZ", isinstance(s.get("ptz"), dict) and "X" in s["ptz"], str(s.get("ptz")))
    except OSError as e:
        check("canal JSON", False, str(e))

    print("10) Vuelta al centro y ajustes por defecto")
    c3.cmd("RATE 500")
    c3.cmd("LIMY 944 1833")
    c3.cmd("X 1500")
    c3.cmd("Y 1500")
    c3.wait_still("X")
    c3.wait_still("Y")
    for line in ("IDLE 3000", "WDT 0", "LIMX 500 2500", "DETACH"):
        c3.cmd(line)
    st = c3.status()
    check("centro, sueltos, valores por defecto", st and st["X"] == 1500 and st["Y"] == 1500
          and st["attX"] == 0 and st["attY"] == 0 and st["rateX"] == 500 and st["idle"] == 3000, str(st))
    c3.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host", nargs="?")
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--port", type=int, default=5000)
    ap.add_argument("--ctrl-port", type=int, default=5300)
    a = ap.parse_args()
    if not a.simulate and not a.host:
        ap.error("indica la IP de la Pi o --simulate")
    proc = None
    host = a.host
    if a.simulate:
        cfg = os.path.join(tempfile.mkdtemp(prefix="asicam-"), "settings.json")
        proc = subprocess.Popen([sys.executable, SERVER, "--simulate", "--bind", "127.0.0.1", "--config", cfg,
                                 "--ptz", "on", "--ptz-port", str(a.port)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        host = "127.0.0.1"
    try:
        run(host, a.port, a.ctrl_port)
    finally:
        if proc:
            proc.terminate()
            proc.wait(10)
    bad = [n for n, ok in results if not ok]
    print("\nRESUMEN: %d pruebas, %d fallas%s" % (len(results), len(bad), (": " + "; ".join(bad)) if bad else ""))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
