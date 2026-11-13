#!/usr/bin/env python3
"""Verifica un servidor asicam de punta a punta, sin paquetes extra.

Uso:
    python asicam/tools/check_server.py                    # lo busca en la red
    python asicam/tools/check_server.py 192.168.200.159    # directo a esa IP
    python asicam/tools/check_server.py HOST --quick       # solo lo basico
    python asicam/tools/check_server.py HOST --stress      # incluye prueba de recuperacion

Al terminar deja los ajustes del servidor como estaban.
"""

import argparse
import json
import socket
import struct
import sys
import threading
import time

CTRL_PORT = 5300
DATA_PORT = 5301
HDR = struct.Struct("<4sHHIdIHHHBBIHHh2xI")
IMG_NAMES = {0: "RAW8", 2: "RAW16"}
BPP = {0: 1, 2: 2}

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print("  [%s] %s%s" % ("OK " if ok else "FALLA", name, (" - " + detail) if detail else ""), flush=True)
    return ok


def discover(timeout=1.5, target=None):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.3)
    # 255.255.255.255 puede salir por otra interfaz (p. ej. las virtuales de Hyper-V):
    # tambien se manda al broadcast /24 de cada IP local.
    dests = [("255.255.255.255", CTRL_PORT)]
    try:
        local = {ai[4][0] for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except OSError:
        local = set()
    for ip in local:
        if not ip.startswith("127."):
            dests.append((ip.rsplit(".", 1)[0] + ".255", CTRL_PORT))
    if target:
        dests.append((target, CTRL_PORT))
    for d in dests:
        try:
            s.sendto(b"ASICAM?", d)
        except OSError:
            pass
    found = {}
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            data, addr = s.recvfrom(2048)
        except socket.timeout:
            continue
        if data.startswith(b"ASICAM "):
            try:
                found[addr[0]] = json.loads(data[7:].decode())
            except ValueError:
                pass
    s.close()
    return found


class Control:
    def __init__(self, host, port=CTRL_PORT):
        self.sock = socket.create_connection((host, port), timeout=10)
        self.f = self.sock.makefile("rwb")
        self.n = 0

    def cmd(self, cmd, **kw):
        self.n += 1
        req = dict(kw, cmd=cmd, id=self.n)
        self.f.write((json.dumps(req) + "\n").encode())
        self.f.flush()
        resp = json.loads(self.f.readline().decode())
        if resp.get("id") != self.n:
            raise RuntimeError("respuesta con id equivocado: %r" % resp)
        return resp

    def raw(self, line):
        self.f.write(line + b"\n")
        self.f.flush()
        return json.loads(self.f.readline().decode())

    def close(self):
        self.sock.close()


class Stream:
    """Recibe cuadros en un hilo y guarda las cabeceras (y el ultimo cuadro)."""

    def __init__(self, host, port=DATA_PORT):
        self.sock = socket.create_connection((host, port), timeout=15)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 << 20)
        self.f = self.sock.makefile("rb", buffering=1 << 22)
        self.lock = threading.Lock()
        self.headers = []
        self.last_payload = b""
        self.error = None
        self.running = True
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        try:
            while self.running:
                h = self.f.read(HDR.size)
                if len(h) < HDR.size:
                    break
                fields = HDR.unpack(h)
                if fields[0] != b"ASIF":
                    raise RuntimeError("cabecera invalida %r" % fields[0])
                payload = self.f.read(fields[-1])
                if len(payload) < fields[-1]:
                    break
                rec = dict(zip(("magic", "version", "hdr_size", "seq", "ts", "exposure_us", "gain",
                                "width", "height", "bin", "img_type", "dropped", "restarts", "flags",
                                "temp10", "size"), fields))
                rec["rx"] = time.monotonic()
                with self.lock:
                    self.headers.append(rec)
                    self.last_payload = payload
        except Exception as e:  # noqa: BLE001
            if self.running:
                self.error = e

    def take(self):
        with self.lock:
            h, self.headers = self.headers, []
            return h

    def wait_frames(self, n, timeout):
        end = time.monotonic() + timeout
        got = []
        while time.monotonic() < end and len(got) < n:
            time.sleep(0.05)
            got += self.take()
        return got

    def wait_for(self, pred, timeout):
        """Espera un cuadro que cumpla pred. Devuelve (cuadro, segundos) o (None, timeout)."""
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            for r in self.take():
                if pred(r):
                    return r, time.monotonic() - t0
            time.sleep(0.05)
        return None, timeout

    def close(self):
        self.running = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


def measure(stream, secs):
    stream.take()
    time.sleep(secs)
    hs = stream.take()
    if len(hs) < 2:
        return hs, 0.0, 0
    gaps = sum(b["seq"] - a["seq"] - 1 for a, b in zip(hs, hs[1:]) if b["seq"] > a["seq"] + 1)
    fps = (len(hs) - 1) / (hs[-1]["rx"] - hs[0]["rx"])
    return hs, fps, gaps


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host", nargs="?")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--stress", action="store_true", help="fuerza perdidas para probar la recuperacion")
    ap.add_argument("--secs", type=float, default=10.0)
    a = ap.parse_args()

    print("1) Busqueda en la red")
    found = discover(target=a.host)
    for ip, info in found.items():
        print("     %s -> %s" % (ip, info))
    host = a.host or (next(iter(found)) if found else None)
    check("responde a la busqueda (UDP %d)" % CTRL_PORT, bool(found))
    if not host:
        sys.exit("no encontre ningun servidor asicam")

    print("2) Canal de control (%s:%d)" % (host, CTRL_PORT))
    ctl = Control(host)
    hello = ctl.cmd("hello")
    check("hello", hello.get("ok") and hello.get("server") == "asicam",
          "version %s, SDK %s" % (hello.get("version"), hello.get("sdk")))
    check("cabecera de cuadro de %d bytes" % HDR.size, hello.get("frame_header", {}).get("size") == HDR.size)
    info = ctl.cmd("info")
    check("info de la camara", info.get("ok"), info.get("camera", {}).get("name", info.get("error", "")))
    cam = info.get("camera", {})
    if info.get("ok"):
        print("     %dx%d, formatos %s, bins %s, %s bits" % (cam["max_width"], cam["max_height"],
                                                          cam["formats"], cam["bins"], cam["bit_depth"]))
        for k, c in sorted(info["controls"].items()):
            print("     %-16s %-24s %8d..%-8d def %-6d%s" % (k, c["name"], c["min"], c["max"], c["default"],
                                                            " auto" if c["auto"] else ""))
    original = ctl.cmd("get")["settings"]
    check("get", isinstance(original, dict) and "exposure_us" in original)
    st = ctl.cmd("status")
    check("status", st.get("ok"), "estado %s" % st.get("state"))
    bad = ctl.cmd("set", settings={"exposure_us": -5})
    check("rechaza un valor fuera de rango", bad.get("ok") is False, bad.get("error", ""))
    bad = ctl.cmd("set", settings={"no_existe": 1})
    check("rechaza un ajuste desconocido", bad.get("ok") is False, bad.get("error", ""))
    bad = ctl.raw(b"esto no es json")
    check("rechaza una linea invalida sin cortar la conexion", bad.get("ok") is False and ctl.cmd("hello")["ok"])

    try:
        base = {"image_type": "RAW8", "bin": 1, "width": 0, "height": 0, "start_x": -1, "start_y": -1,
                "exposure_us": 10000, "auto_exposure": False, "gain": 0, "auto_gain": False}
        r = ctl.cmd("set", settings=base)
        check("set de ajustes base (1080p RAW8, 10 ms)", r.get("ok"), r.get("error", ""))

        print("3) Cuadros por el canal de datos (%s:%d)" % (host, DATA_PORT))
        t0 = time.monotonic()
        stream = Stream(host)
        first = stream.wait_frames(1, 15)
        check("llega el primer cuadro", bool(first), "%.1f s desde conectar" % (time.monotonic() - t0))
        if not first:
            raise RuntimeError("sin cuadros: %s" % ctl.cmd("status"))
        h = first[0]
        check("tamano 1920x1080 RAW8", (h["width"], h["height"], h["img_type"], h["size"]) ==
              (cam["max_width"], cam["max_height"], 0, cam["max_width"] * cam["max_height"]),
              "%dx%d %s %d bytes" % (h["width"], h["height"], IMG_NAMES.get(h["img_type"]), h["size"]))
        check("hora del cuadro coherente", abs(h["ts"] - time.time()) < 5,
              "diferencia con el reloj del PC %.3f s" % (h["ts"] - time.time()))
        secs = 4 if a.quick else a.secs
        hs, fps, gaps = measure(stream, secs)
        st = ctl.cmd("status")
        check("flujo continuo %.0f s" % secs, fps > 5 and gaps == 0,
              "%.2f fps recibidos, %d huecos, servidor %.2f fps, %s, descartes SDK %d, reinicios %d" % (
                  fps, gaps, st["fps"], st["state"], st["dropped_sdk"], st["restarts"]))
        check("estado capturando con 1 cliente", st["state"] == "capturando" and len(st["clients"]) == 1)
        if st.get("temperature_c") is not None:
            print("     temperatura del sensor %.1f C" % st["temperature_c"])

        print("4) Cambios en vivo")
        r = ctl.cmd("set", settings={"exposure_us": 5000})
        f, dt = stream.wait_for(lambda x: x["exposure_us"] == 5000, 5)
        check("exposicion 10 -> 5 ms se refleja en los cuadros", f is not None, "%.2f s" % dt)
        r = ctl.cmd("set", settings={"gain": 120})
        f, dt = stream.wait_for(lambda x: x["gain"] == 120, 5)
        check("ganancia 0 -> 120 se refleja", f is not None, "%.2f s" % dt)
        # Arranca muy corta para que el modo automatico tenga que subirla
        ctl.cmd("set", settings={"exposure_us": 100, "gain": 0})
        stream.wait_for(lambda x: x["exposure_us"] == 100, 5)
        r = ctl.cmd("set", settings={"auto_exposure": True})
        f, dt = stream.wait_for(lambda x: x["flags"] & 1, 5)
        check("exposicion automatica activada (marca en la cabecera)", f is not None,
              "exposicion leida %s us" % (f["exposure_us"] if f else "-"))
        time.sleep(4)
        hs = stream.take()
        exps = [x["exposure_us"] for x in hs]
        check("el modo automatico cambia la exposicion", len(set(exps)) > 1,
              "de %s a %s us en 4 s (%d valores distintos)" % (exps[0] if exps else "-", exps[-1] if exps else "-",
                                                              len(set(exps))))
        ctl.cmd("set", settings={"auto_exposure": False, "exposure_us": 10000, "gain": 0})
        f, dt = stream.wait_for(lambda x: not (x["flags"] & 1) and x["exposure_us"] == 10000, 5)
        check("vuelve a exposicion manual 10 ms", f is not None)

        if not a.quick:
            print("5) Cambios de geometria")
            for desc, sets, exp in (
                ("bin 2 -> 960x540", {"bin": 2}, (960, 540, 0, 2)),
                ("ROI 1280x720 bin 1", {"bin": 1, "width": 1280, "height": 720}, (1280, 720, 0, 1)),
                ("ROI 640x480 en esquina (0,0)", {"width": 640, "height": 480, "start_x": 0, "start_y": 0},
                 (640, 480, 0, 1)),
                ("RAW16 1080p", {"width": 0, "height": 0, "start_x": -1, "start_y": -1, "image_type": "RAW16"},
                 (1920, 1080, 2, 1)),
            ):
                ctl.cmd("set", settings=sets)
                f, dt = stream.wait_for(lambda x, e=exp: (x["width"], x["height"], x["img_type"], x["bin"]) == e, 10)
                ok = f is not None and f["size"] == exp[0] * exp[1] * BPP[exp[2]]
                hs, fps, gaps = measure(stream, 3) if ok else ([], 0, 0)
                check(desc, ok, "cambio en %.2f s, luego %.1f fps" % (dt, fps) if ok else "no llego")
                if f is not None and desc.startswith("ROI 640"):
                    st = ctl.cmd("status")
                    check("posicion del ROI (0,0)", st["geometry"]["start_x"] == 0 and st["geometry"]["start_y"] == 0,
                          str(st["geometry"]))
            ctl.cmd("set", settings=base)
            f, dt = stream.wait_for(lambda x: (x["width"], x["img_type"]) == (1920, 0), 10)
            check("vuelve a 1080p RAW8", f is not None)

            print("6) Dos clientes de datos a la vez")
            s2 = Stream(host)
            got2 = s2.wait_frames(5, 10)
            hs, fps, gaps = measure(stream, 3)
            check("segundo cliente recibe y el primero sigue", len(got2) >= 5 and fps > 5,
                  "cliente 2: %d cuadros; cliente 1: %.1f fps" % (len(got2), fps))
            s2.close()

        if a.stress:
            print("7) Recuperacion ante perdidas (ancho de banda USB 90 %)")
            st0 = ctl.cmd("status")
            ctl.cmd("set", settings={"bandwidth": 90})
            time.sleep(20)
            hs, fps, gaps = measure(stream, 10)
            st1 = ctl.cmd("status")
            print("     reinicios %d -> %d, descartes SDK %d -> %d, %.1f fps en los ultimos 10 s" % (
                st0["restarts"], st1["restarts"], st0["dropped_sdk"], st1["dropped_sdk"], fps))
            ctl.cmd("set", settings={"bandwidth": original.get("bandwidth", 60)})
            f, dt = stream.wait_frames(5, 15), 0
            hs, fps, gaps = measure(stream, 8)
            check("sigue entregando cuadros tras las fallas", fps > 5,
                  "%.1f fps con ancho de banda %s %%" % (fps, original.get("bandwidth", 60)))

        print("8) Al desconectar se suelta la camara")
        stream.close()
        t0 = time.monotonic()
        state = None
        while time.monotonic() - t0 < 15:
            state = ctl.cmd("status")["state"]
            if state == "inactiva":
                break
            time.sleep(0.5)
        check("camara liberada sin clientes", state == "inactiva", "%.1f s" % (time.monotonic() - t0))
        if stream.error:
            check("sin errores en el flujo", False, repr(stream.error))
    finally:
        keys = {k: v for k, v in original.items()}
        r = ctl.cmd("set", settings=keys)
        print("ajustes restaurados: %s" % ("si" if r.get("ok") else r))
        ctl.close()

    bad = [n for n, ok in results if not ok]
    print("\nRESUMEN: %d pruebas, %d fallas%s" % (len(results), len(bad), (": " + "; ".join(bad)) if bad else ""))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
