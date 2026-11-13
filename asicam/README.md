# asicam: la Raspberry como relay de video de una cámara ZWO ASI

Módulo aparte del proyecto ufomount (se fusionará más adelante con el control PTZ).
La Raspberry captura los cuadros **crudos** de la cámara (RAW8/RAW16, sin comprimir)
y los reenvía por red. Todo el procesamiento se hace en el PC que se conecta.

Probado con una **ZWO ASI220MM Mini** en una Raspberry Pi 4 con Astroberry
(Raspbian Buster de 32 bits, Python 3.7) y el SDK de ZWO 1.39.

## Estado

| Fase | Contenido | Estado |
|---|---|---|
| 1 | Servidor en la Pi: captura, control, datos, búsqueda | **hecha y verificada** |
| 2 | Librería cliente en el PC (cuadros como arreglos numpy) | pendiente |
| 3 | Interfaz en el PC con ajustes tipo ASIStudio | pendiente |
| 4 | Robustez: driver 1.26 para exposiciones largas, prueba larga | pendiente |
| 5 | Servicio en la Pi (arranque automático, tiempo real) | pendiente |

## Archivos

```
asicam/
  pi/asicam_server.py     servidor (corre en la Raspberry)
  pi/zwo_sdk.py           acceso al SDK de ZWO por ctypes
  tools/check_server.py   verificación de punta a punta desde el PC
```

El servidor solo usa la biblioteca estándar de Python (3.7 o más nuevo) y el SDK de ZWO
(`libASICamera2.so`). No instala nada.

## Uso en la Raspberry

Copiar los archivos (desde la raíz del repositorio, en el PC):

```bash
scp asicam/pi/asicam_server.py asicam/pi/zwo_sdk.py astroberry@astroberry.local:asicam/
```

Arrancar el servidor en la Pi:

```bash
cd ~/asicam && python3 asicam_server.py
```

Con prioridad de tiempo real (permite más fps sin perder cuadros; requiere root). Usar un
`--config` propio para que root no se quede con el archivo de ajustes del usuario:

```bash
cd ~/asicam && sudo python3 asicam_server.py --rt --config /home/astroberry/.config/asicam/settings-rt.json
```

Opciones: `--sdk RUTA` (o variable `ASICAM_SDK`) elige la biblioteca de ZWO. Por defecto usa
la 1.39 de `~/scripts/ASI_linux_mac_SDK_V1.39/lib/armv7/` si existe, y si no, la del sistema.
Los ajustes se guardan en `~/.config/asicam/settings.json` y se recuperan al reiniciar.

**La cámara se abre solo mientras hay clientes de datos conectados.** Unos 5 s después de que
se va el último cliente, el servidor la suelta y queda libre para PHD2, KStars o INDI.

## Verificación desde el PC

```bash
python asicam/tools/check_server.py
```

```bash
python asicam/tools/check_server.py 192.168.200.159 --stress
```

Sin IP, busca el servidor en la red. Con IP, va directo. `--quick` hace solo lo básico;
`--stress` además fuerza pérdidas de cuadros para probar la recuperación automática.
Al terminar deja los ajustes como estaban.

## Protocolo

| Puerto | Uso |
|---|---|
| 5300/UDP | búsqueda: responde a `ASICAM?` |
| 5300/TCP | control: una línea JSON por pedido y una por respuesta |
| 5301/TCP | datos: cuadros crudos, cada uno con una cabecera binaria |

### Búsqueda (UDP 5300)

Mandar `ASICAM?` (broadcast o directo). Respuesta: `ASICAM ` seguido de un JSON:

```json
{"server": "asicam", "version": "0.1.0", "host": "astroberry", "camera": "ZWO ASI220MM Mini",
 "control": 5300, "data": 5301, "state": "inactiva"}
```

En Windows conviene mandarlo también al broadcast de la red (`192.168.200.255`), porque
`255.255.255.255` puede salir por una interfaz virtual (Hyper-V, WSL).

### Control (TCP 5300)

Cada pedido es un objeto JSON en una línea, con `cmd` y opcionalmente `id` (se devuelve igual
en la respuesta). Toda respuesta trae `ok`; si es `false`, trae `error`. Una línea inválida no
corta la conexión.

| Comando | Qué hace |
|---|---|
| `{"cmd": "hello"}` | versión del servidor y del SDK, puertos y tamaño de la cabecera de cuadro |
| `{"cmd": "info"}` | datos de la cámara (tamaño, formatos, bins, bits) y rango de cada ajuste |
| `{"cmd": "get"}` | ajustes actuales |
| `{"cmd": "set", "settings": {...}}` | cambia uno o más ajustes; se valida todo antes de aplicar |
| `{"cmd": "status"}` | estado, fps, cuadros, descartes, reinicios, temperatura, clientes |

Ejemplo:

```
-> {"cmd": "set", "settings": {"exposure_us": 27000, "gain": 0}, "id": 7}
<- {"ok": true, "settings": {...todos los ajustes...}, "id": 7}
```

### Ajustes

Los cambios de exposición, ganancia y demás controles se aplican **en vivo** (en 0,2 s
aproximadamente). Los de geometría (formato, bin, tamaño, posición) reinician la captura
(menos de 1 s).

| Ajuste | Qué es | ASI220MM Mini |
|---|---|---|
| `image_type` | `RAW8` o `RAW16` | ambos |
| `bin` | agrupamiento de píxeles | 1 o 2 |
| `width`, `height` | tamaño del recorte; `0` = el máximo | ancho múltiplo de 8, alto par |
| `start_x`, `start_y` | esquina del recorte; `-1` = centrado | |
| `exposure_us`, `auto_exposure` | exposición en microsegundos | 32 µs a 10 s |
| `gain`, `auto_gain` | ganancia | 0 a 600 |
| `offset` | nivel de negro | 0 a 1500 |
| `bandwidth`, `auto_bandwidth` | ancho de banda USB en % | 40 a 100 |
| `high_speed` | modo High Speed (0/1) | **dejar en 0**: en 1 limita a ~10 fps |
| `flip` | 0 nada, 1 horizontal, 2 vertical, 3 ambos | |
| `auto_max_gain`, `auto_max_exp_ms`, `auto_target` | límites del modo automático | |
| `gamma`, `wb_r`, `wb_b`, `auto_wb` | solo cámaras que los tengan | no aplica (mono) |

`null` en un control significa "el valor por defecto de la cámara".

### Datos (TCP 5301)

El cliente se conecta y solo lee; no manda nada. Cada cuadro es una cabecera de 48 bytes
(little-endian, `struct` de Python `"<4sHHIdIHHHBBIHHh2xI"`) seguida de los píxeles:

| Campo | Tipo | Contenido |
|---|---|---|
| magic | 4 bytes | `ASIF` |
| version | uint16 | 1 |
| header_size | uint16 | 48 |
| seq | uint32 | número de cuadro desde que arrancó el servidor (un salto = cuadro perdido en la red) |
| timestamp | float64 | hora Unix en que la Pi recibió el cuadro (reloj de la Pi, sincronizado por NTP) |
| exposure_us | uint32 | exposición del cuadro (la real si está en automático) |
| gain | uint16 | ganancia del cuadro |
| width, height | uint16 | tamaño en píxeles |
| bin | uint8 | bin |
| image_type | uint8 | 0 = RAW8, 2 = RAW16 |
| dropped | uint32 | cuadros que el SDK descartó en total |
| restarts | uint16 | reinicios automáticos de la captura |
| flags | uint16 | bit 0 = exposición automática, bit 1 = ganancia automática |
| temp10 | int16 | temperatura del sensor × 10 (−32768 = sin dato) |
| (relleno) | 2 bytes | |
| payload_size | uint32 | bytes de píxeles que siguen |

Píxeles: fila por fila, sin relleno. RAW8 = 1 byte por píxel. RAW16 = 2 bytes little-endian;
la ASI220 entrega 12 bits **alineados arriba** (valores múltiplos de 16: dividir por 16 para
tener 0 a 4095).

Si un cliente lee más lento que la cámara, se descartan cuadros solo para ese cliente (cola de
3); los demás no se afectan.

## Recuperación ante fallas

La ASI220 Mini no tiene memoria propia: si la Pi se atrasa al leer el USB, se pierde un cuadro
y la captura puede quedar trabada. Tras 3 lecturas fallidas seguidas, el servidor reinicia la
captura sola (menos de 1 s de corte) y lo cuenta en `restarts`. Si la cámara se desconecta,
reintenta abrirla cada 2 s.

## Rendimiento medido (1080p RAW8, Pi 4, red gigabit)

| Configuración | fps | Resultado |
|---|---|---|
| Prioridad normal, ancho de banda 60 % | 12,0 | 20 s sin huecos (1 reinicio automático, sin pérdida en el cliente) |
| Tiempo real, ancho de banda 70 % | 14,0 | 60 s sin huecos ni reinicios |
| RAW16 1080p | 6,1 | |
| Recorte 1280×720 / 640×480 | 25 / 37 | |
| Ancho de banda 90 % (forzado) | ~5 | pierde cuadros, el servidor se recupera solo |

El crudo 1080p a 14 fps ocupa unos 230 Mbit/s: necesita red **gigabit**. Un inyector PoE
pasivo de 2 pares limita a 100 Mbps.

Pendiente (fase 4): con el SDK 1.39 las exposiciones de 110 ms o más no entregan cuadros; con
el 1.26 del sistema sí.
