# Continuar ufomount en otro equipo

Punto de partida para usar o seguir desarrollando el proyecto sin contexto previo. Estado al
**24 de noviembre de 2026**.

## Qué es y dónde está cada parte

Una montura PTZ (dos servos GX3350) con una cámara ZWO ASI220MM Mini. Una **Raspberry Pi 4**
mueve los servos y transmite la cámara; el PC muestra y controla todo por red.

Todo está en la rama **`main`**:

| Carpeta | Qué tiene |
|---|---|
| `app/` | Programa UFOMOUNT del PC: montura, cámara y conexión sin router |
| `asicam/` | Servidor de la Raspberry (`pi/`), librería e interfaz de cámara del PC (`pc/`), pruebas |
| `android/` | App Android UFOMOUNT Foco, para enfocar la cámara desde el teléfono |
| `firmware/`, `scripts/` | Arduino y su interfaz PTZ: **solo respaldo**, no se trabaja |
| `docs/` | Esta guía, el esquema de la Raspberry e informes |

Las ramas `fusion`, `modulo-asicam` y `apk-foco` son de antes de juntar todo en `main` y ya no se
usan. Los ejecutables están en [Releases](https://github.com/GGm4nS3c/ufomount/releases), no en el
repo: las `v5.0-beta` son de la Raspberry y la `v4.1` del Arduino.

## Solo usar el programa

1. Descargar `Ufomount.exe` de la beta más nueva en Releases. No hace falta Python. No está
   firmado: si SmartScreen avisa, *Más información → Ejecutar de todas formas*.
2. La montura toma su IP por **DHCP** (por cable es solo cliente):
   - con router: **Buscar**. Si la Pi está en otra red, escribir una IP de esa red;
   - sin router: **Conexión directa (sin router)**, ver [app/README.md](../app/README.md).
3. La montura y la cámara **no piden clave**: usarlas solo en una red de confianza, con una clave
   propia en la Raspberry y en su hotspot.

## Preparar un PC para desarrollar

Windows 10/11, [Git](https://git-scm.com) y **Python 3.12** (probado con 3.12.10).

```bash
git clone https://github.com/GGm4nS3c/ufomount.git
```

```bash
cd ufomount
```

```bash
python -m pip install numpy pyserial pygame pyinstaller
```

Versiones con las que se compiló: numpy 2.5.4, pyserial 3.5, pygame 2.6.1, PyInstaller 6.22.0.

Comprobar que todo funciona, **sin la montura**:

```bash
python app/ufomount.py --simulate --selftest
```

```bash
python app/test_direct.py
```

Deben dar `24 pruebas, 0 fallas` y `0 fallas`. Compilar:

```bash
python -m PyInstaller --noconfirm --onefile --windowed --name Ufomount --paths scripts --paths asicam/pc --paths app app/ufomount.py
```

El `.exe` no admite `--simulate` (intenta lanzar el simulador con Python). Se prueba contra un
simulador aparte, en dos terminales:

```bash
python asicam/pi/asicam_server.py --simulate --bind 127.0.0.1
```

```bash
dist/Ufomount.exe --selftest --host 127.0.0.1
```

Sin `--simulate` y con la IP de la Pi, `--selftest` **mueve los servos**.

La app Android necesita además el JDK 17 y el SDK de Android: ver [android/README.md](../android/README.md).

### Lo que no está en el repo

- **Llave de firma de la APK** (`ufomount-foco.keystore`): copiarla a mano. Sin ella la app
  instalada no se puede actualizar sin desinstalarla. No subirla: el repo es público.
- **Acceso SSH a la Raspberry**: cada PC usa su propia llave o la clave de la Pi.
- **SDK de ZWO** (`libASICamera2.so`): ya está en la Raspberry; no se distribuye aquí.
- `scripts/led_web.py`: prueba del LED del Arduino, nunca se subió.

## La Raspberry

- Pi 4 con Astroberry (Raspbian Buster de 32 bits, **Python 3.7**): el código de `asicam/pi` debe
  servir en 3.7.
- El servicio `asicam` arranca solo. Puertos: PTZ TCP/UDP **5000**, control TCP/UDP **5300**, datos
  TCP **5301**. Instalación, cableado y protocolo: [asicam/README.md](../asicam/README.md).
- Puede responder en **dos IP** con la misma MAC: tiene dos clientes DHCP (dhcpcd y NetworkManager).
- El hotspot WiFi de Astroberry reparte sus propias IP (10.42.0.x); no afecta a la red por cable.
- Mientras se transmite, PHD2 e INDI no pueden abrir la cámara.

Después de cambiar `asicam/pi`, copiarlo a la Pi y reiniciar el servicio:

```bash
scp asicam/pi/asicam_server.py asicam/pi/zwo_sdk.py asicam/pi/ptz.py astroberry@astroberry.local:asicam/
```

```bash
ssh -t astroberry@astroberry.local "sudo systemctl restart asicam"
```

## Estado

| Parte | Estado |
|---|---|
| Servidor de la Pi: cámara, servos, servicio | Probado con la montura real |
| Programa UFOMOUNT, tres modos y Buscar en otras redes | Probado con la montura real y con el simulador |
| App Android | Probada por WiFi en un teléfono real (14 fps completa, 37 fps enfoque) |
| **Conexión directa sin router (v0.2.1)** | Solo con clientes DHCP falsos. **Falta la montura real** |

### Conexión directa: dónde quedó

- **Beta 1 (0.2.0)** falló en otro PC con `WinError 10013` al escuchar en el UDP 67. Ese error se
  reproduce cuando otro programa tiene reservado para sí el puerto en todas las IP.
- **Beta 2 (0.2.1)** usa entonces una segunda vía (lee los pedidos DHCP desde los paquetes IP, con
  permisos de administrador) y muestra qué programa tiene el puerto. **No se ha probado**: ni la
  segunda vía, ni el cambio de IP con `netsh`, ni el permiso de Windows con la montura.
- Si falla, el texto del recuadro de la ventana dice en qué paso. Código: `app/direct_link.py`.

## Pendientes

1. Probar la conexión directa con la montura.
2. Enfocar una escena con luz con la app Android, y buscar desde el teléfono con la Pi en otra red.
3. Fase 4: con el SDK 1.39 las exposiciones de **110 ms o más no entregan cuadros** (con el 1.26
   sí); falta además una prueba larga de estabilidad.
4. La Pi puede contestar la búsqueda UDP desde su otra IP y el firewall de Windows descarta esa
   respuesta; por eso la conexión automática prueba TCP 5300. Falta que conteste desde la misma IP.
5. Enfoque motorizado (idea): 28BYJ-48 con ULN2003 y correa GT2. Falta saber qué lente es y cuánto
   gira el anillo. Los dos canales de PWM por hardware ya están ocupados.
6. Más adelante: mapas y calibración al norte en el modo Montura.

## Si la montura anda pero la cámara no conecta

Los comandos de la montura son mínimos y pasan por una red mala; el video no. Pasó el 24 de
noviembre, con la Pi detrás de un switch a unos 10 m:

| Síntoma | Medida |
|---|---|
| Enlace de la Pi | 50 cortes en 40 min; a 1 Gbps duraba 1 s |
| Paquetes grandes (1472 bytes) | 10–25 % perdidos; los chicos, 0 % |
| Sentido de la pérdida | Solo lo que la Pi envía |
| Voltaje y duplex de la Pi | Bien (`throttled=0x0`, full) |

Tras reiniciar todo quedó a 1 Gbps, 0 % de pérdida y 11 MB/s. Se supuso que era el cable, pero se
reinició todo a la vez: **no está demostrado**. Si vuelve, cambiar una sola cosa cada vez: primero
la Pi con un cable corto y sin PoE, después el cable entre switches, después el PoE.

Cómo mirar (solo lectura):

```bash
ping -n 50 -l 1472 IP_DE_LA_PI
```

```bash
ssh astroberry@astroberry.local "vcgencmd get_throttled; cat /sys/class/net/eth0/speed /sys/class/net/eth0/carrier_changes; dmesg | grep 'eth0: Link' | tail"
```

- El inyector PoE pasivo de 2 pares deja la red en **100 Mbps** (unos 5 fps a 1080p completo). Los
  14 fps necesitan 1 Gbps **en todo el camino**, incluido el PC.
- Orden del PoE pasivo: **switch → inyector → cable → splitter → Pi**. Los 24 V nunca deben llegar a
  un switch.

## Acuerdos de trabajo

- Se trabaja por fases y cada una se aprueba antes de empezar; no se sube nada a GitHub sin permiso.
- Todo se trabaja en `main`.
- El Arduino solo se conserva. En `app/` no se agregan puertos COM ni opciones de Arduino.
- `app/` reutiliza `scripts/ptz_gui.py` y `asicam/pc/asicam_gui.py` **sin modificarlos**.
- Commits y documentación en español.
