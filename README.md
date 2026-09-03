# ufomount

Montura PTZ (pan-tilt) para cámara, controlada por Arduino UNO con dos
servos brushless GXservo GX3350ProBLS "X50 Pro":

- **Eje X (pan): 270°** — señal en D9
- **Eje Y (tilt): 180°** — señal en D10

Interfaz de escritorio en Python para el control fino (zoom) y el
seguimiento rápido (aviones), con posición en vivo, velocidades
configurables y log de actividad.

## Estructura

| Ruta | Qué es |
|---|---|
| `firmware/ptztest/` | Firmware del UNO (v3): protocolo serie, rampas de velocidad, auto-detach antizumbido, watchdog de enlace |
| `scripts/ptz_gui.py` | Interfaz gráfica (tkinter + pyserial): jog, presets de velocidad, diales de posición, logs |
| `scripts/*.ps1` | Utilidades: verificación guiada, jog por consola, medición de consumo |
| `docs/GX3350-informe.md` | Informe técnico de los servos (specs, PWM, alimentación) |

## Uso rápido

```powershell
# Firmware (una vez): compilar y subir con arduino-cli
arduino-cli compile --fqbn arduino:avr:uno firmware/ptztest
arduino-cli upload -p COM5 --fqbn arduino:avr:uno firmware/ptztest

# Interfaz
pip install pyserial
python scripts/ptz_gui.py
```

## Conexionado

| Cable del servo | Va a |
|---|---|
| Franja blanca (señal) | D9 (X) / D10 (Y) |
| Central (V+) | Fuente externa 6,0–8,4 V (nunca el 5V del Arduino) |
| Restante (GND) | Negativo de fuente + GND del Arduino (tierra común) |

Fuente recomendada para ambos ejes: ≥10 A a 6 V o ≥12 A a 7,4 V.
Detalles, protocolo serie completo y fuentes en
[docs/GX3350-informe.md](docs/GX3350-informe.md).

## Paquete portable

`python -m PyInstaller --onefile --windowed --name UfomountPTZ scripts/ptz_gui.py`
genera el ejecutable standalone (los binarios no se versionan; ver
Releases si hay alguno publicado).
