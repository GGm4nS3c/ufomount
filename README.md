# ufomount

Montura PTZ (pan-tilt) para cámara, controlada por Arduino UNO con shield
Ethernet y dos servos brushless GXservo GX3350ProBLS "X50 Pro":

- **Eje X (pan): 270°** — señal en D2
- **Eje Y (tilt): 180°** — señal en D3

Interfaz de escritorio en Python para el control fino (zoom) y el
seguimiento rápido (aviones), con posición en vivo, velocidades
configurables y log de actividad. Se conecta por USB o por red.

## Estructura

| Ruta | Qué es |
|---|---|
| `firmware/ptztest/` | Firmware del UNO (v4): protocolo de líneas por USB y TCP, rampas de velocidad, auto-detach antizumbido, watchdog de enlace |
| `scripts/ptz_gui.py` | Interfaz gráfica (tkinter + pyserial): jog, presets de velocidad, diales de posición, logs |
| `scripts/*.ps1` | Utilidades por USB: verificación guiada, jog por consola, medición de consumo |
| `docs/GX3350-informe.md` | Informe técnico de los servos (specs, PWM, alimentación) |

## Uso rápido

```powershell
# Firmware (una vez): librerías y carga con arduino-cli
arduino-cli lib install Servo Ethernet
arduino-cli compile --fqbn arduino:avr:uno firmware/ptztest
arduino-cli upload -p COM5 --fqbn arduino:avr:uno firmware/ptztest

# Interfaz
pip install pyserial
python scripts/ptz_gui.py                        # USB (COM5)
python scripts/ptz_gui.py --port 192.168.200.50  # red
```

En la GUI, el desplegable de puerto ofrece los COM detectados y
`192.168.200.50:5000`; también se puede escribir otra `IP:puerto`.

## Límites del tilt (eje Y)

Para que la cámara no tope, el eje Y solo recorre de **-50°** (suelo) a
**+30°** (cielo), con 0° = horizonte (1500 µs).

- El firmware arranca con ese rango (`LIMY 944 1833`), así queda protegido
  aunque no haya GUI conectada.
- En la GUI, *Límites tilt Y* permite cambiarlo; se guarda en
  `~/.ufomount_ptz.json` y se envía al firmware en cada conexión. *Por
  defecto* vuelve a -50°/+30°.
- El dial del tilt muestra en verde el recorrido disponible y en rojo el
  bloqueado.

## Control por red

| Parámetro | Valor (en `ptztest.ino`) |
|---|---|
| IP fija | 192.168.200.50 / 255.255.255.0, gateway 192.168.200.254 |
| Puerto TCP | 5000 |
| MAC | 02:55:46:4F:00:01 (administrada localmente) |

- Mismo protocolo que por USB (`PING`, `X <us>`, `STATUS`…), una línea por
  comando. `NET` informa el chip de la shield, el enlace y el cliente.
- USB y red funcionan a la vez. Una conexión TCP a la vez: una nueva
  reemplaza a la anterior.
- Si la conexión TCP que movía la montura se cae, o su canal deja de
  hablar con el watchdog activo, la montura se detiene.
- Por red el UNO **no** se resetea al conectar (por USB sí): conserva su
  posición.
- Prueba manual: PuTTY en modo *Raw* a 192.168.200.50, puerto 5000.
- Sin autenticación: pensado para una LAN de confianza.

## Conexionado

| Cable del servo | Va a |
|---|---|
| Franja blanca (señal) | D2 (X) / D3 (Y) |
| Central (V+) | Fuente externa 6,0–8,4 V (nunca el 5V del Arduino) |
| Restante (GND) | Negativo de fuente + GND del Arduino (tierra común) |

La shield Ethernet ocupa D10 (chip-select), D11–D13 (SPI) y D4 (SD): no
conectar servos ahí. Si la shield tiene soldado el puente de interrupción
(INT) a D2, ese pin también queda ocupado.

Si el RJ45 de la shield se apoya en el USB del UNO, la shield no calza a
fondo y los pines de ese extremo (D8–D10) pueden quedar sin contacto: usar
extensores de pines o cinta aislante sobre el USB.

Fuente recomendada para ambos ejes: ≥10 A a 6 V o ≥12 A a 7,4 V.
Detalles, protocolo serie completo y fuentes en
[docs/GX3350-informe.md](docs/GX3350-informe.md).

## Paquete portable

`python -m PyInstaller --onefile --windowed --name UfomountPTZ scripts/ptz_gui.py`
genera el ejecutable standalone (los binarios no se versionan; ver
Releases si hay alguno publicado).
