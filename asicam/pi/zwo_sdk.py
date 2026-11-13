"""Acceso minimo al SDK de ZWO (libASICamera2) por ctypes.

No necesita paquetes extra: solo la libreria del SDK (.so en Linux, .dll en
Windows). Compatible con Python 3.7 (Raspbian Buster de la Pi).

Solo un hilo debe usar una Camera a la vez: el servidor hace todas las
llamadas al SDK desde su hilo de captura.
"""

import ctypes as C
import os

# Tipos de imagen del SDK
IMG_RAW8, IMG_RGB24, IMG_RAW16, IMG_Y8 = 0, 1, 2, 3
IMG_END = -1
IMG_NAMES = {IMG_RAW8: "RAW8", IMG_RGB24: "RGB24", IMG_RAW16: "RAW16", IMG_Y8: "Y8"}
BYTES_PER_PIXEL = {IMG_RAW8: 1, IMG_RGB24: 3, IMG_RAW16: 2, IMG_Y8: 1}

# Tipos de control del SDK (ASI_CONTROL_TYPE)
GAIN = 0
EXPOSURE = 1
GAMMA = 2
WB_R = 3
WB_B = 4
OFFSET = 5
BANDWIDTH = 6
OVERCLOCK = 7
TEMPERATURE = 8
FLIP = 9
AUTO_MAX_GAIN = 10
AUTO_MAX_EXP = 11
AUTO_TARGET_BRIGHTNESS = 12
HARDWARE_BIN = 13
HIGH_SPEED_MODE = 14

# Controles que se prueban aunque el SDK no los liste: (tipo, nombre, min, max)
HIDDEN_CONTROLS = [(HIGH_SPEED_MODE, "HighSpeedMode", 0, 1)]

# Codigos de error del SDK (ASI_ERROR_CODE)
OK = 0
ERR_TIMEOUT = 11
ERR_CAMERA_CLOSED = 4
ERR_CAMERA_REMOVED = 5
ERRORS = {
    0: "ok",
    1: "indice de camara invalido",
    2: "id de camara invalido",
    3: "tipo de control invalido",
    4: "camara cerrada",
    5: "camara desconectada",
    6: "ruta invalida",
    7: "formato de archivo invalido",
    8: "tamano invalido",
    9: "tipo de imagen invalido",
    10: "fuera de rango",
    11: "tiempo de espera agotado",
    12: "secuencia invalida",
    13: "buffer muy chico",
    14: "modo video activo",
    15: "exposicion en curso",
    16: "error general",
    17: "modo invalido",
}


class ASIError(Exception):
    def __init__(self, what, code):
        self.code = code
        super().__init__("%s: %s (codigo %d)" % (what, ERRORS.get(code, "desconocido"), code))


def _chk(code, what):
    if code != OK:
        raise ASIError(what, code)


class _CameraInfo(C.Structure):
    _fields_ = [("Name", C.c_char * 64), ("CameraID", C.c_int),
                ("MaxHeight", C.c_long), ("MaxWidth", C.c_long),
                ("IsColorCam", C.c_int), ("BayerPattern", C.c_int),
                ("SupportedBins", C.c_int * 16), ("SupportedVideoFormat", C.c_int * 8),
                ("PixelSize", C.c_double), ("MechanicalShutter", C.c_int),
                ("ST4Port", C.c_int), ("IsCoolerCam", C.c_int),
                ("IsUSB3Host", C.c_int), ("IsUSB3Camera", C.c_int),
                ("ElecPerADU", C.c_float), ("BitDepth", C.c_int),
                ("IsTriggerCam", C.c_int), ("Unused", C.c_char * 16)]


class _ControlCaps(C.Structure):
    _fields_ = [("Name", C.c_char * 64), ("Description", C.c_char * 128),
                ("MaxValue", C.c_long), ("MinValue", C.c_long), ("DefaultValue", C.c_long),
                ("IsAutoSupported", C.c_int), ("IsWritable", C.c_int),
                ("ControlType", C.c_int), ("Unused", C.c_char * 32)]


class SDK:
    """La libreria del SDK cargada, con los tipos de argumentos declarados."""

    def __init__(self, path=None):
        path = path or ("ASICamera2.dll" if os.name == "nt" else "libASICamera2.so")
        self.path = path
        L = self.lib = C.CDLL(path)
        i, pi, lng, pl = C.c_int, C.POINTER(C.c_int), C.c_long, C.POINTER(C.c_long)
        L.ASIGetSDKVersion.restype = C.c_char_p
        L.ASIGetNumOfConnectedCameras.argtypes = []
        L.ASIGetCameraProperty.argtypes = [C.POINTER(_CameraInfo), i]
        for fn in ("ASIOpenCamera", "ASIInitCamera", "ASICloseCamera",
                   "ASIStartVideoCapture", "ASIStopVideoCapture"):
            getattr(L, fn).argtypes = [i]
        L.ASIGetNumOfControls.argtypes = [i, pi]
        L.ASIGetControlCaps.argtypes = [i, i, C.POINTER(_ControlCaps)]
        L.ASIGetControlValue.argtypes = [i, i, pl, pi]
        L.ASISetControlValue.argtypes = [i, i, lng, i]
        L.ASISetROIFormat.argtypes = [i, i, i, i, i]
        L.ASIGetROIFormat.argtypes = [i, pi, pi, pi, pi]
        L.ASISetStartPos.argtypes = [i, i, i]
        L.ASIGetStartPos.argtypes = [i, pi, pi]
        L.ASIGetVideoData.argtypes = [i, C.POINTER(C.c_ubyte), lng, i]
        L.ASIGetDroppedFrames.argtypes = [i, pi]
        self.version = L.ASIGetSDKVersion().decode().replace(", ", ".")

    def num_cameras(self):
        return self.lib.ASIGetNumOfConnectedCameras()

    def open(self, index=0):
        """Abre e inicializa la camara numero `index`."""
        return Camera(self, index)


class Camera:
    """Una camara ZWO abierta. Cerrarla con close()."""

    def __init__(self, sdk, index=0):
        self.sdk = sdk
        L = sdk.lib
        if index >= sdk.num_cameras():
            raise ASIError("abrir camara", 1)
        ci = _CameraInfo()
        _chk(L.ASIGetCameraProperty(C.byref(ci), index), "leer propiedades")
        self.id = ci.CameraID
        _chk(L.ASIOpenCamera(self.id), "abrir camara")
        try:
            _chk(L.ASIInitCamera(self.id), "iniciar camara")
        except ASIError:
            L.ASICloseCamera(self.id)
            raise
        self.closed = False
        formats = []
        for f in ci.SupportedVideoFormat:  # la lista termina en IMG_END
            if f == IMG_END:
                break
            if f in IMG_NAMES and IMG_NAMES[f] not in formats:
                formats.append(IMG_NAMES[f])
        self.info = {
            "name": ci.Name.decode(errors="replace"),
            "max_width": int(ci.MaxWidth),
            "max_height": int(ci.MaxHeight),
            "color": bool(ci.IsColorCam),
            "bayer": int(ci.BayerPattern),
            "bins": [b for b in ci.SupportedBins if b > 0],
            "formats": formats,
            "pixel_um": round(ci.PixelSize, 3),
            "bit_depth": int(ci.BitDepth),
            "usb3_host": bool(ci.IsUSB3Host),
            "usb3_camera": bool(ci.IsUSB3Camera),
            "e_per_adu": round(float(ci.ElecPerADU), 4),
        }
        self.controls = {}
        n = C.c_int()
        _chk(L.ASIGetNumOfControls(self.id, C.byref(n)), "contar controles")
        for k in range(n.value):
            cc = _ControlCaps()
            if L.ASIGetControlCaps(self.id, k, C.byref(cc)) != OK:
                continue
            self.controls[int(cc.ControlType)] = {
                "name": cc.Name.decode(errors="replace"),
                "description": cc.Description.decode(errors="replace"),
                "min": int(cc.MinValue),
                "max": int(cc.MaxValue),
                "default": int(cc.DefaultValue),
                "auto": bool(cc.IsAutoSupported),
                "writable": bool(cc.IsWritable),
                "hidden": False,
            }
        # Algunos SDK no listan controles que si aceptan (p. ej. el 1.39 no lista
        # HighSpeedMode de la ASI220 Mini, pero lo aplica). Se agregan si responden.
        for ctl, name, lo, hi in HIDDEN_CONTROLS:
            if ctl in self.controls:
                continue
            v, a = C.c_long(), C.c_int()
            if L.ASIGetControlValue(self.id, ctl, C.byref(v), C.byref(a)) == OK:
                self.controls[ctl] = {"name": name, "description": "control no listado por el SDK",
                                      "min": lo, "max": hi, "default": int(v.value), "auto": False,
                                      "writable": True, "hidden": True}

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.sdk.lib.ASIStopVideoCapture(self.id)
            finally:
                self.sdk.lib.ASICloseCamera(self.id)

    def has(self, ctl):
        return ctl in self.controls

    def set_control(self, ctl, value, auto=False):
        _chk(self.sdk.lib.ASISetControlValue(self.id, ctl, int(value), 1 if auto else 0),
             "ajustar %s" % self.controls.get(ctl, {}).get("name", ctl))

    def get_control(self, ctl):
        v, a = C.c_long(), C.c_int()
        _chk(self.sdk.lib.ASIGetControlValue(self.id, ctl, C.byref(v), C.byref(a)),
             "leer %s" % self.controls.get(ctl, {}).get("name", ctl))
        return int(v.value), bool(a.value)

    def set_roi(self, width, height, binning, img_type, start_x=None, start_y=None):
        L = self.sdk.lib
        _chk(L.ASISetROIFormat(self.id, width, height, binning, img_type), "ajustar ROI")
        if start_x is not None and start_y is not None:
            _chk(L.ASISetStartPos(self.id, start_x, start_y), "ajustar posicion del ROI")

    def get_roi(self):
        w, h, b, t = C.c_int(), C.c_int(), C.c_int(), C.c_int()
        _chk(self.sdk.lib.ASIGetROIFormat(self.id, C.byref(w), C.byref(h), C.byref(b), C.byref(t)),
             "leer ROI")
        x, y = C.c_int(), C.c_int()
        _chk(self.sdk.lib.ASIGetStartPos(self.id, C.byref(x), C.byref(y)), "leer posicion del ROI")
        return {"width": w.value, "height": h.value, "bin": b.value, "img_type": t.value,
                "start_x": x.value, "start_y": y.value}

    def start_video(self):
        _chk(self.sdk.lib.ASIStartVideoCapture(self.id), "iniciar video")

    def stop_video(self):
        _chk(self.sdk.lib.ASIStopVideoCapture(self.id), "detener video")

    def read_frame(self, buf, timeout_ms):
        """Lee un cuadro en `buf` (arreglo ctypes). Devuelve el codigo del SDK (0 = ok)."""
        return self.sdk.lib.ASIGetVideoData(self.id, buf, len(buf), int(timeout_ms))

    def dropped_frames(self):
        d = C.c_int()
        self.sdk.lib.ASIGetDroppedFrames(self.id, C.byref(d))
        return d.value
