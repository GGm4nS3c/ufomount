# UFOMOUNT: montura y cámara en un solo programa

Programa único para usar la montura ufomount con la **Raspberry**: la Pi maneja los servos
(PWM por hardware) y transmite la cámara ZWO ASI220MM Mini. El Arduino queda como respaldo
con `UfomountPTZ.exe` (rama `main`).

Al abrir aparece un menú con tres modos (teclas 1, 2, 3):

| Modo | Qué muestra |
|---|---|
| **Montura** | Solo el control PTZ: velocidades, posición de los ejes X e Y (diales), ir a, límites del tilt, mando. Se conecta solo si la Raspberry responde. Aquí irán mapas, calibración al norte, etc. |
| **Cámara** | Solo la cámara: vista en vivo, histograma, exposición, ganancia, formato, foto FITS/PGM y video `.ser`. |
| **Montura completa** | Cámara y montura en una ventana. Una sola barra: **Buscar** encuentra la Raspberry y conecta; **Conectar** / **Desconectar** manejan las dos. La montura funciona aunque la Pi no tenga la cámara enchufada; la cámara se reintenta sola cada 8 s. La división entre los paneles se arrastra. |

El botón **◂ Menú** vuelve al menú en cualquier modo; al salir de un modo la montura recibe STOP.

**Buscar** recorre la red local y también las redes donde ya se usó la Pi. Si la Pi está en
otra red (por ejemplo detrás de un router, en `192.168.201.x`), escribir una IP de esa red
(`192.168.201.0`) en el campo **Raspberry** (o **Puerto** en el modo Montura) y pulsar
**Buscar**: recorre esa red completa y conecta al encontrarla. Después la recuerda.

Teclado: las **flechas** mueven la montura y **Espacio** la detiene, salvo mientras se escribe
en un campo (exposición, puerto…). Un clic en cualquier otra parte devuelve las flechas a la
montura.

## Pantalla completa (modo Montura completa)

**Doble clic en la imagen** (o el botón *Pantalla completa*, o F11) muestra solo el video, a
pantalla completa, y la montura se sigue manejando:

| Acción | Cómo |
|---|---|
| Mover | flechas del teclado, mando, o los botones ▲ ▼ ◀ ▶ abajo a la derecha (mantener apretado) |
| Parar | Espacio, botón STOP o B del mando |
| Velocidad | `+` / `−` (Fino 2 °/s, Normal 8 °/s, Avión 25 °/s) o los botones − + |
| Salir | Esc o doble clic en la imagen |

Arriba a la derecha se ven el pan y el tilt en grados, la velocidad y el estado de la montura.

## Uso

```bash
python app/ufomount.py
```

```bash
python app/ufomount.py --mode full --fullscreen
```

Opciones: `--mode ptz|cam|full` abre directo ese modo; `--host IP` elige la Raspberry;
`--fullscreen` (con `--mode full`) arranca en pantalla completa; `--simulate` levanta un
servidor simulado local (cámara y servos) para probar sin la Pi.

Necesita **numpy** y **pyserial**; el mando es opcional (**pygame**). Recuerda el último modo
en `~/.ufomount_app.json`; la cámara y la montura guardan sus ajustes como siempre
(`~/.asicam_gui.json`, `~/.ufomount_ptz.json`).

## Cómo está hecho

No modifica las interfaces existentes: `scripts/ptz_gui.py` (montura) y
`asicam/pc/asicam_gui.py` (cámara) se usan tal cual, cada una dentro de un contenedor de esta
ventana (`Embed`). Así los otros proyectos siguen funcionando solos y las mejoras de cada
interfaz llegan aquí sin copiar código. La montura se usa solo por red: en este programa no
hay puertos COM.

## Prueba automática

```bash
python app/ufomount.py --simulate --selftest
```

Recorre el menú y los tres modos como un usuario (24 pruebas): conexión, foco del teclado, flechas, doble clic a
pantalla completa, video en pantalla completa, botones sobre la imagen, velocidad, Esc. Sin
`--simulate` se prueba contra la Pi real: **mueve los servos** un poco y los deja centrados.

## Ejecutable

```bash
python -m PyInstaller --noconfirm --onefile --windowed --name Ufomount --paths scripts --paths asicam/pc app/ufomount.py
```
