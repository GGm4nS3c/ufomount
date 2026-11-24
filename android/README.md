# UFOMOUNT Foco (Android)

App pequeña para **enfocar la cámara desde el teléfono**, junto a la montura: busca la
Raspberry en la red, muestra la cámara en vivo y da un **índice de nitidez** para girar el anillo
de enfoque hasta el máximo. Usa el mismo servidor de la Pi que el PC (`asicam`); en la Pi no
cambia nada.

APK: `android/foco/build/UfomountFoco.apk` (también se copia a `dist/`). Android 8 o más nuevo.

## Instalar

- **Copiando el archivo**: pasar `UfomountFoco.apk` al teléfono (cable, Drive, mail…) y
  abrirlo. Android pide permitir "instalar apps de origen desconocido" para la app con la
  que se abre (Archivos, Chrome…): es normal para apps que no vienen de la tienda.
- **Por USB** (con la depuración USB activada en el teléfono):

```bash
python android/foco/build.py --install
```

## Usar

1. El teléfono tiene que estar en una red que llegue a la Pi (la misma WiFi, o una que pase
   por el router).
2. **Buscar**: encuentra la Raspberry por broadcast y recorriendo la red del teléfono. Si la Pi
   está en otra red, escribir una IP de esa red (p. ej. `192.168.201.0`) y pulsar **Buscar**.
   La app recuerda la última Pi y se conecta sola al abrirla.
3. Modos de vista:
   - **Completa**: toda la imagen, reducida a la mitad (bin 2, 960×540).
   - **Enfoque (centro 1:1)**: un recorte central de 640×480 a resolución real, píxel por
     píxel. Es el mejor para enfocar.
4. **Nitidez**: el número sube al acercarse al foco. Girar el anillo despacio: cuando el
   número llega a su máximo (barra verde) la cámara está enfocada; si se pasa, baja.
   **Reiniciar máx.** vuelve a empezar (p. ej. al cambiar de objeto o de exposición).
5. **Exposición** y **Ganancia**, con Auto. De noche hay que subirlas: con la imagen negra
   la nitidez no sirve.
6. Dos dedos: zoom. Doble toque: acercar o volver a ajustar. **Cruz**: líneas centrales.

Mientras la app está conectada, la cámara usa su modo (los ajustes son los mismos para todos
los clientes, así que el PC también ve ese recorte). **Al salir o pasar a segundo plano, la
app devuelve la cámara a los ajustes que tenía.** Esos ajustes quedan guardados en el teléfono
hasta haberlos devuelto: si la app se cierra a la fuerza o se corta la red, los devuelve en la
próxima conexión. La pantalla no se apaga mientras está abierta.

Probado en un Samsung Galaxy S22 Ultra (Android 16) contra la Pi real: búsqueda por WiFi sin
nada guardado (la encuentra y se conecta en unos 2 s), video por WiFi en los dos modos
(Completa 14 fps, Enfoque 37 fps: el máximo de la cámara), exposición, nitidez, salir y
volver, y cierre forzado. Falta probar en el teléfono la búsqueda con la Pi en otra red (en
el PC funciona).

El índice es la energía de los bordes del centro de la imagen, normalizada por el brillo
(en pantalla, multiplicada por 1000):
sirve para paisajes y para estrellas, y casi no cambia con la exposición. Es relativo: hay
que compararlo contra sí mismo en la misma escena, no entre escenas distintas.

## Compilar

Sin Android Studio ni Gradle: solo el JDK 17 y el SDK de Android (línea de comandos).

Herramientas (una vez, ~1,1 GB en `C:\Android`):
- JDK 17 (Eclipse Temurin, zip) en `C:\Android\jdk-17`.
- Android command-line tools en `C:\Android\sdk\cmdline-tools\latest`, y con su
  `sdkmanager`: `platforms;android-35`, `build-tools;35.0.0`, `platform-tools`
  (hay que aceptar la licencia del SDK).

Otras rutas: variables `JAVA_HOME` y `ANDROID_HOME`.

```bash
python android/foco/build.py
```

Pasos: `aapt2` (recursos y manifiesto) → `javac` (Java 8) → `d8` (dex) → `zipalign` →
`apksigner`. Firma con una llave propia que se crea la primera vez **fuera del repo**
(`C:\Android\ufomount-foco.keystore`); hay que conservarla para poder actualizar la app sin
desinstalarla.

## Prueba

El núcleo (`src/com/ufomount/foco/core`: búsqueda, protocolo, nitidez) no depende de
Android, así que se prueba en el PC contra la Pi real: busca, se conecta, pasa por los dos
modos, cambia exposición y ganancia, comprueba el índice de nitidez y deja la cámara como
estaba (18 pruebas). No mueve la montura.

```bash
python android/foco/build.py --test 192.168.201.0
```

## Archivos

```
android/foco/
  AndroidManifest.xml
  build.py                 compilacion sin Gradle (y --test, --install)
  res/                     nombre e icono
  src/com/ufomount/foco/
    MainActivity.java      pantalla
    ZoomView.java          imagen con zoom
    FocusBar.java          barra de nitidez
    core/                  sin Android: MiniJson, Discovery, AsiCamLink, Frame, Focus
  test/DesktopTest.java    prueba del nucleo en el PC
```
