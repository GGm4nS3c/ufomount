#!/usr/bin/env python3
"""Pruebas de la libreria cliente (asicam/pc/asicam_client.py).

Uso:
    python asicam/tools/test_client.py --simulate          # levanta un servidor simulado local
    python asicam/tools/test_client.py 192.168.200.159     # contra la Pi real
    python asicam/tools/test_client.py HOST --restart-cmd "COMANDO"   # incluye reconexion

--restart-cmd es un programa con sus argumentos (sin shell, rutas con /) que reinicia
el servidor, para probar que el cliente se reconecta solo. Con --simulate la
reconexion se prueba siempre.
Al terminar deja los ajustes del servidor como estaban.
"""

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "pc"))
import numpy as np  # noqa: E402

from asicam_client import AsiCam, AsiCamError, discover, save_fits, save_pgm  # noqa: E402

SERVER = os.path.join(HERE, "..", "pi", "asicam_server.py")
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print("  [%s] %s%s" % ("OK " if ok else "FALLA", name, (" - " + detail) if detail else ""), flush=True)
    return ok


def read_fits(path):
    with open(path, "rb") as f:
        raw = f.read()
    cards = {}
    pos = 0
    while True:
        card = raw[pos:pos + 80].decode("ascii")
        pos += 80
        if card.startswith("END"):
            break
        if card[8:10] == "= ":
            val = card[10:].split(" / ")[0].strip()
            cards[card[:8].strip()] = val.strip("'").strip()
    pos += -pos % 2880
    w, h, bitpix = int(cards["NAXIS1"]), int(cards["NAXIS2"]), int(cards["BITPIX"])
    if bitpix == 8:
        data = np.frombuffer(raw, np.uint8, w * h, pos).reshape(h, w)
    else:
        data = (np.frombuffer(raw, ">i2", w * h, pos).astype(np.int32) + int(cards.get("BZERO", 0)))
        data = data.astype(np.uint16).reshape(h, w)
    return cards, data


class LocalServer:
    """Servidor simulado en 127.0.0.1 (para probar sin la Pi)."""

    def __init__(self):
        self.cfg = os.path.join(tempfile.mkdtemp(prefix="asicam-"), "settings.json")
        self.p = None
        self.start()

    def start(self):
        self.p = subprocess.Popen([sys.executable, SERVER, "--simulate", "--bind", "127.0.0.1",
                                   "--config", self.cfg], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)

    def restart(self):
        self.stop()
        time.sleep(1.0)
        self.start()

    def stop(self):
        if self.p and self.p.poll() is None:
            self.p.terminate()
            self.p.wait(10)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("host", nargs="?")
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--restart-cmd")
    a = ap.parse_args()
    local = LocalServer() if a.simulate else None
    host = "127.0.0.1" if a.simulate else a.host
    restart = local.restart if local else (
        (lambda: subprocess.run(shlex.split(a.restart_cmd), check=False)) if a.restart_cmd else None)
    try:
        run(host, restart)
    finally:
        if local:
            local.stop()
    bad = [n for n, ok in results if not ok]
    print("\nRESUMEN: %d pruebas, %d fallas%s" % (len(results), len(bad), (": " + "; ".join(bad)) if bad else ""))
    sys.exit(1 if bad else 0)


def run(host, restart):
    print("1) Busqueda")
    found = discover(targets=[host] if host else [])
    print("     %s" % [(s["ips"], s.get("host"), s.get("camera")) for s in found])
    host = host or (found[0]["ip"] if found else None)
    if not host:
        check("discover() encuentra el servidor", False)
        return

    print("2) Control")
    cam = AsiCam(host, queue_size=4)
    check("conecta y saluda", cam.server.get("server") == "asicam", "servidor %s, SDK %s, equipo %s" % (
        cam.server.get("version"), cam.server.get("sdk"), cam.server.get("host")))
    # la Pi puede tener varias IP y contestar la busqueda desde otra: se compara por nombre
    check("discover() encuentra este servidor", any(s.get("host") == cam.server.get("host") for s in found))
    info = cam.info()
    camd = info["camera"]
    check("info()", camd.get("max_width", 0) > 0, "%s, %d bits" % (camd["name"], camd["bit_depth"]))
    original = cam.settings()
    try:
        cam.set(gain=-1)
        check("set() con valor invalido lanza AsiCamError", False)
    except AsiCamError as e:
        check("set() con valor invalido lanza AsiCamError", True, str(e))
    s = cam.set(image_type="RAW8", bin=1, width=0, height=0, start_x=-1, start_y=-1,
                exposure_us=10000, auto_exposure=False, gain=0, auto_gain=False)
    check("set() devuelve los ajustes aplicados", s["exposure_us"] == 10000 and s["gain"] == 0)

    try:
        print("3) Cuadros")
        seen = []
        cam.on_frame(lambda f: seen.append(f.seq))
        cam.start()
        f = cam.get_frame(timeout=15)
        check("get_frame() entrega un cuadro", f is not None, repr(f))
        if f is None:
            return
        check("imagen numpy 1080x1920 uint8", f.image.shape == (camd["max_height"], camd["max_width"])
              and f.image.dtype == np.uint8, "%s %s" % (f.image.shape, f.image.dtype))
        check("metadatos del cuadro", f.exposure_us == 10000 and f.gain == 0 and f.image_type == "RAW8"
              and not f.auto_exposure, "temp %s C, latencia %.3f s" % (f.temperature_c, f.latency))
        time.sleep(5)
        st = cam.stats()
        n_before = len(seen)
        drained = 0
        while cam.get_frame(timeout=0) is not None:
            drained += 1
        check("flujo continuo", st["fps"] > 5 and st["gaps"] == 0,
              "%.1f fps, %d recibidos, %d huecos, %.0f Mbit/s" % (
                  st["fps"], st["received"], st["gaps"], st["bytes"] * 8 / 6.0 / 1e6))
        check("callback on_frame() llamado por cada cuadro", n_before >= st["received"] - 1)
        lat = cam.latest()
        check("latest() devuelve el mas reciente", lat is not None and lat.seq >= f.seq)

        print("4) Cliente lento: se quedan los cuadros mas nuevos")
        d0 = cam.stats()["dropped_local"]
        time.sleep(2.0)  # sin leer
        newest = cam.latest().seq
        g = cam.get_frame(timeout=1)
        d1 = cam.stats()["dropped_local"]
        check("descarta los viejos sin cortar el flujo", d1 > d0 and g is not None and newest - g.seq <= 4,
              "descartados %d, cuadro entregado #%d, ultimo #%d" % (d1 - d0, g.seq if g else -1, newest))

        print("5) Formatos y geometria")
        for desc, sets, shape, dtype in (
                ("RAW16 1080p", {"image_type": "RAW16"}, (1080, 1920), np.uint16),
                ("bin 2", {"image_type": "RAW8", "bin": 2}, (540, 960), np.uint8),
                ("ROI 1280x720", {"bin": 1, "width": 1280, "height": 720}, (720, 1280), np.uint8)):
            cam.set(**sets)
            t0 = time.monotonic()
            got = None
            while time.monotonic() - t0 < 10:
                fr = cam.get_frame(timeout=1)
                if fr is not None and fr.image.shape == shape and fr.image.dtype == dtype:
                    got = fr
                    break
            check(desc, got is not None, "%s %s en %.2f s" % (shape, np.dtype(dtype).name, time.monotonic() - t0))
            if got is not None and dtype == np.uint16:
                px = got.pixels()
                check("RAW16: 12 bits alineados arriba, pixels() da 0..4095",
                      int((got.image % 16).max()) == 0 and int(px.max()) <= 4095,
                      "crudo max %d, pixels() max %d" % (got.image.max(), px.max()))
                raw16 = got
        cam.set(image_type="RAW8", bin=1, width=0, height=0)

        print("6) Guardar cuadros")
        tmp = tempfile.mkdtemp(prefix="asicam-snap-")
        fr = None
        t0 = time.monotonic()
        while time.monotonic() - t0 < 10:  # los cuadros en cola pueden ser de la geometria anterior
            fr = cam.get_frame(timeout=1)
            if fr is not None and fr.image.shape == (camd["max_height"], camd["max_width"]):
                break
        for fobj, label in ((fr, "RAW8"), (locals().get("raw16"), "RAW16")):
            if fobj is None:
                continue
            p = os.path.join(tmp, "f_%s.fits" % label)
            save_fits(p, fobj)
            cards, data = read_fits(p)
            check("FITS %s se lee igual" % label, np.array_equal(data, fobj.image) and
                  abs(float(cards["EXPTIME"]) - fobj.exposure_us / 1e6) < 1e-9 and os.path.getsize(p) % 2880 == 0,
                  "%d bytes, EXPTIME=%s DATE-OBS=%s" % (os.path.getsize(p), cards["EXPTIME"], cards["DATE-OBS"]))
            p = os.path.join(tmp, "f_%s.pgm" % label)
            save_pgm(p, fobj)
            with open(p, "rb") as fh:
                head = fh.read(20)
            check("PGM %s" % label, head.startswith(b"P5\n%d %d\n" % (fobj.width, fobj.height)))

        if restart:
            print("7) Reconexion cuando el servidor se reinicia")
            r0 = cam.stats()["reconnects"]
            restart()
            t0 = time.monotonic()
            ok = False
            while time.monotonic() - t0 < 30:
                if cam.stats()["reconnects"] > r0 and cam.get_frame(timeout=1) is not None:
                    ok = True
                    break
                time.sleep(0.2)
            check("el flujo vuelve solo", ok, "%.1f s" % (time.monotonic() - t0))
            try:
                st = cam.status()
                check("el canal de control vuelve solo", st.get("ok"), "estado %s" % st.get("state"))
            except AsiCamError as e:
                check("el canal de control vuelve solo", False, str(e))
    finally:
        cam.stop()
        try:
            cam.set(**original)
            print("ajustes restaurados")
        except AsiCamError as e:
            print("no pude restaurar ajustes: %s" % e)
        cam.close()


if __name__ == "__main__":
    main()
