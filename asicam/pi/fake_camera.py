"""Camara simulada con la misma interfaz que zwo_sdk, para probar sin hardware.

Imita una ASI220MM Mini: 1920x1080 mono, RAW8/RAW16 (12 bits alineados arriba),
bin 1/2, los mismos controles y rangos. Genera ruido cuyo brillo depende de la
exposicion y la ganancia, con un punto brillante que se mueve. Con el ancho de
banda USB en 80 % o mas a veces se "traba" como la camara real, para probar la
recuperacion del servidor.

    python3 asicam_server.py --simulate --bind 127.0.0.1
"""

import ctypes
import os
import random
import time

import zwo_sdk as Z

W, H = 1920, 1080


def _caps(name, lo, hi, default, auto=False, writable=True, hidden=False):
    return {"name": name, "description": name, "min": lo, "max": hi, "default": default,
            "auto": auto, "writable": writable, "hidden": hidden}


class FakeSDK:
    version = "simulado"
    path = "simulado"

    def __init__(self, fps=14.0):
        self.fps = fps
        self.present = True

    def num_cameras(self):
        return 1 if self.present else 0

    def open(self, index=0):
        if index >= self.num_cameras():
            raise Z.ASIError("abrir camara", 1)
        return FakeCamera(self)


class FakeCamera:
    def __init__(self, sdk):
        self.sdk = sdk
        self.closed = False
        self.info = {"name": "Simulador ASI220MM Mini", "max_width": W, "max_height": H, "color": False,
                     "bayer": 0, "bins": [1, 2], "formats": ["RAW8", "RAW16"], "pixel_um": 4.0,
                     "bit_depth": 12, "usb3_host": False, "usb3_camera": False, "e_per_adu": 1.0}
        self.controls = {
            Z.GAIN: _caps("Gain", 0, 600, 100, auto=True),
            Z.EXPOSURE: _caps("Exposure", 32, 10000000, 10000, auto=True),
            Z.OFFSET: _caps("Offset", 0, 1500, 100),
            Z.BANDWIDTH: _caps("BandWidth", 40, 100, 50, auto=True),
            Z.TEMPERATURE: _caps("Temperature", -500, 1000, 20, writable=False),
            Z.FLIP: _caps("Flip", 0, 3, 0),
            Z.AUTO_MAX_GAIN: _caps("AutoExpMaxGain", 0, 600, 300),
            Z.AUTO_MAX_EXP: _caps("AutoExpMaxExpMS", 1, 10000, 100),
            Z.AUTO_TARGET_BRIGHTNESS: _caps("AutoExpTargetBrightness", 50, 160, 100),
            Z.HIGH_SPEED_MODE: _caps("HighSpeedMode", 0, 1, 0, hidden=True),
        }
        self.values = {c: [v["default"], False] for c, v in self.controls.items()}
        self.values[Z.TEMPERATURE] = [250, False]
        self.roi = {"width": W, "height": H, "bin": 1, "img_type": Z.IMG_RAW8, "start_x": 0, "start_y": 0}
        self.video = False
        self.dropped = 0
        self.stuck = False
        self.n = 0
        self.next_due = 0.0
        rnd = os.urandom(W * H * 2 + 8192)
        self._noise = rnd                                   # bytes uniformes 0..255
        self._low16 = rnd[::-1].translate(bytes(i & 0xF0 for i in range(256)))
        self._tables = {}

    # --- interfaz de zwo_sdk.Camera ---
    def close(self):
        self.closed = True
        self.video = False

    def has(self, ctl):
        return ctl in self.controls

    def set_control(self, ctl, value, auto=False):
        if ctl not in self.controls:
            raise Z.ASIError("ajustar control", 3)
        c = self.controls[ctl]
        self.values[ctl] = [max(c["min"], min(c["max"], int(value))), bool(auto and c["auto"])]

    def get_control(self, ctl):
        if ctl not in self.controls:
            raise Z.ASIError("leer control", 3)
        if ctl == Z.TEMPERATURE:
            self.values[ctl][0] = 250 + int(5 * random.random())
        v, a = self.values[ctl]
        return v, a

    def set_roi(self, width, height, binning, img_type, start_x=None, start_y=None):
        if binning not in self.info["bins"] or img_type not in (Z.IMG_RAW8, Z.IMG_RAW16):
            raise Z.ASIError("ajustar ROI", 9)
        maxw, maxh = W // binning, H // binning
        if width % 8 or height % 2 or not (0 < width <= maxw) or not (0 < height <= maxh):
            raise Z.ASIError("ajustar ROI", 8)
        x = (maxw - width) // 2 if start_x is None else start_x
        y = (maxh - height) // 2 if start_y is None else start_y
        if x < 0 or y < 0 or x + width > maxw or y + height > maxh:
            raise Z.ASIError("ajustar posicion del ROI", 10)
        self.roi = {"width": width, "height": height, "bin": binning, "img_type": img_type,
                    "start_x": x, "start_y": y}

    def get_roi(self):
        return dict(self.roi)

    def start_video(self):
        if self.closed:
            raise Z.ASIError("iniciar video", 4)
        self.video = True
        self.stuck = False
        self.next_due = time.monotonic()

    def stop_video(self):
        self.video = False

    def dropped_frames(self):
        return self.dropped

    def read_frame(self, buf, timeout_ms):
        if self.closed:
            return Z.ERR_CAMERA_CLOSED
        timeout = timeout_ms / 1000.0
        if not self.video:
            time.sleep(min(timeout, 0.05))
            return Z.ERR_TIMEOUT
        r = self.roi
        # ritmo: ~14 fps a 1080p, mas rapido con menos filas, la mitad en RAW16
        rows = r["height"] * r["bin"]
        period = (1.0 / self.sdk.fps) * rows / H
        if r["img_type"] == Z.IMG_RAW16:
            period *= 2
        exp_s = self.values[Z.EXPOSURE][0] / 1e6
        period = max(period, exp_s)
        bw = self.values[Z.BANDWIDTH][0]
        if not self.stuck and bw >= 80 and random.random() < 0.05 * (bw - 75) / 5:
            self.stuck = True
        if self.stuck:  # como la camara real: sin cuadros hasta reiniciar el video
            self.dropped += 1
            time.sleep(timeout)
            return Z.ERR_TIMEOUT
        now = time.monotonic()
        wait = self.next_due - now
        if wait > timeout:
            time.sleep(timeout)
            return Z.ERR_TIMEOUT
        if wait > 0:
            time.sleep(wait)
        self.next_due = max(self.next_due, now) + period
        self._fill(buf)
        self.n += 1
        self._auto_adjust()
        return Z.OK

    # --- imagen sintetica ---
    def _level(self):
        exp = self.values[Z.EXPOSURE][0]
        gain = self.values[Z.GAIN][0]
        return min(250.0, 4.0 + exp / 400.0 * 10 ** (gain / 200.0))

    def _table(self, mean):
        key = int(mean)
        t = self._tables.get(key)
        if t is None:
            spread = 0.08 + 0.002 * self.values[Z.GAIN][0] ** 0.5 * 10
            t = bytes(max(0, min(255, int(mean + (b - 128) * spread))) for b in range(256))
            self._tables[key] = t
        return t

    def _fill(self, buf):
        r = self.roi
        npx = r["width"] * r["height"]
        off = (self.n * 7919) % 8192
        level = self._table(self._level())
        px = self._noise[off:off + npx].translate(level)
        if r["img_type"] == Z.IMG_RAW16:
            data = bytearray(npx * 2)
            data[1::2] = px
            data[0::2] = self._low16[off:off + npx]
            data = bytes(data)  # memmove necesita bytes, no bytearray
        else:
            data = px
        ctypes.memmove(buf, data, len(data))
        # punto brillante que recorre la imagen
        bpp = 2 if r["img_type"] == Z.IMG_RAW16 else 1
        sx = (self.n * 13) % max(1, r["width"] - 8)
        sy = (self.n * 7) % max(1, r["height"] - 8)
        base = ctypes.addressof(buf)
        for dy in range(8):
            ctypes.memset(base + ((sy + dy) * r["width"] + sx) * bpp, 0xF0, 8 * bpp)

    def _auto_adjust(self):
        exp, auto = self.values[Z.EXPOSURE]
        if not auto:
            return
        target = self.values[Z.AUTO_TARGET_BRIGHTNESS][0]
        level = self._level()
        max_exp = self.values[Z.AUTO_MAX_EXP][0] * 1000
        new = exp * (target / max(level, 1.0)) ** 0.3
        self.values[Z.EXPOSURE][0] = int(max(32, min(max_exp, new)))
