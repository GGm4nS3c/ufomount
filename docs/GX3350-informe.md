# Informe técnico: Montura PTZ con Arduino y servos GX3350 Pro BLS

**Fecha:** 2026-08-12 (v2, actualizado con la ficha oficial GXservo/Yipinservo del listado de compra — imágenes aportadas por el usuario).
**Convención:** ✅ = confirmado · ⚠️ = discrepancia menor entre fuentes · ❓ = pendiente de verificar en banco.

---

## 1. Identificación — resuelta

✅ Los dos motores son **GXservo / Yipinservo GX3350ProBLS**, nombre comercial **"X50 Pro"**, del fabricante Dongguan Gongxun Power Technology Co., Ltd. (gxservo.com). El listado de AliExpress (ID 1005009825461352, revendido bajo la marca "Stemedu") vende el mismo servo en variantes de **180° y 270°**: el ángulo se configura en fábrica (rango 90–358°), así que **ambas unidades son el mismo modelo** con distinto recorrido preconfigurado. En EE. UU. lo vende Team Integy (~70 USD).

## 2. Especificaciones oficiales (ficha del fabricante)

| Parámetro | Valor | Estado |
|---|---|---|
| Motor | Brushless, digital, encoder de ángulo **magnético** (sin pista de carbón) | ✅ |
| Voltaje de operación | **5,0–8,4 V** (2S LiPo directa admitida) | ✅ |
| Torque de bloqueo | 40–42 kg·cm @6 V · 45–46 @7,4 V · **50 @8,4 V** | ⚠️ tabla/gráfica difieren ±2 |
| Velocidad sin carga | 0,10–0,11 s/60° @6 V · 0,085–0,10 @7,4 V · 0,08–0,09 @8,4 V | ⚠️ ídem |
| **Corriente de bloqueo** | **5 A @6 V · 6 A @7,4 V · 7 A @8,4 V** | ✅ |
| **Corriente con carga** | **2 A @6 V · 3 A @7,4 V · 3,6 A @8,4 V** | ✅ |
| Control | PWM 1520 µs / 333 Hz; pulso **500–2500 µs = recorrido completo**; banda muerta 2 µs | ✅ |
| Nivel de señal PWM | 3,3–5 V (recomendado 5 V) → el UNO (5 V) va perfecto | ✅ |
| Sentido | **Antihorario** al subir de 500 → 2500 µs (invertible por programador) | ✅ |
| Ángulo | Por defecto 180°; **configurable de fábrica/programador 90–358°** (360° = rotación continua sin control de posición) | ✅ |
| Protecciones integradas (iC "Gongxun 2.0") | Soft-start al encender, anti-bloqueo por límite de tensión/corriente, protección térmica | ✅ |
| Mecánica | Engranajes inox 416+304, doble rodamiento, caja CNC, estriado 25T | ✅ |
| Estanqueidad | IP67 (tabla) / IPX8 (ficha X50 Pro) | ⚠️ |
| Dimensiones / peso | 40,5 × 20 × 38 mm · 88 g (ficha) / 95 g (tabla), sin accesorios | ⚠️ |
| Cable | JR, 300 mm | ✅ |
| Temperatura | −20 °C a +60 °C | ✅ |

### Colores de cable (¡ojo, no es la convención JR clásica!)

Según el diagrama del fabricante: **cable con franja blanca = SEÑAL** · **cable central = POSITIVO** · el restante = **NEGATIVO/GND**. (El positivo en el centro coincide con la convención JR, así que un conector invertido no cruza V+ y GND, pero identifica la señal por la franja blanca, no por color naranja.)

## 3. Control desde Arduino

- Librería `Servo` estándar a 50 Hz: válida (el servo acepta desde 50 Hz hasta 333 Hz).
- `attach(pin, 500, 2500)` + `writeMicroseconds()`. En la unidad de 180°: ~11,1 µs/°. En la de 270°: ~7,4 µs/°.
- Limitar recorrido por software acotando el pulso; no salir de 500–2500 µs.
- Señal en cualquier pin digital; en UNO la librería toma el Timer1 y anula `analogWrite()` en pines 9/10 (usamos justo esos: X→D9, Y→D10).
- El **programador USB GX001V3** permite ajustar ángulo, neutro, sentido, velocidad, banda muerta, soft-start y protección de bloqueo — con él se podría reconfigurar el recorrido si algún día hiciera falta. No modificar los parámetros marcados en rojo en su software.
- Sin salida de telemetría de posición (3 hilos, solo entrada PWM), aunque el sensor interno es magnético.

## 4. Alimentación para DOS servos

Con las corrientes oficiales, el presupuesto queda así:

| Voltaje | Bloqueo simultáneo (peor caso) | Carga simultánea (típico) | Fuente recomendada |
|---|---|---|---|
| 6,0 V | 2 × 5 A = 10 A | 2 × 2 A = 4 A | ≥10 A continuos |
| 7,4 V | 2 × 6 A = 12 A | 2 × 3 A = 6 A | **≥12 A continuos** |
| 8,4 V | 2 × 7 A = 14 A | 2 × 3,6 A = 7,2 A | ≥14 A continuos |

- 8,4 V ya está confirmado como seguro, pero para una PTZ (movimientos cortos, sin necesidad del torque máximo) **6,0–7,4 V es el punto dulce**: menos corriente, menos calor.
- El soft-start y la protección anti-bloqueo integrados reducen los picos, pero se dimensiona igual para el peor caso.
- **Nunca desde el pin 5V del Arduino ni USB**; fuente dedicada + **tierra común** con el Arduino.
- Condensador 470–2200 µF low-ESR ≥16 V en el riel de servos (baseline 1000 µF) + 0,1 µF cerámico.
- Potencia en 18–20 AWG; el conector JR aguanta ~3 A continuos — para bloqueos de 6 A conviene alimentar por cables soldados/XT30 y dejar el JR solo para señal, o al menos minimizar extensiones.

## 5. Errores comunes a evitar

1. Brownout/reinicios por alimentar del riel lógico o fuente corta.
2. Saltos completos simultáneos de ambos ejes (picos apilados): attach escalonado, posición inicial escrita antes del attach, rampas con `millis()`.
3. Jitter por tierra mal unida, cables finos o falta de condensadores.
4. Salirse de 500–2500 µs esperando más recorrido.
5. Contar con `analogWrite()` en 9/10 con servos activos (`detach()` no lo restaura — bug #3860).
6. Fijar endpoints sin prueba suave previa de límites y sentido.

## 6. Pendiente de verificar en banco

| Dato | Cómo |
|---|---|
| Cuál unidad física es la de 180° y cuál la de 270° | Prueba suave: ATTACH y barrido corto; el recorrido total lo delata (o etiqueta/pegatina si la hay) |
| Sentido de giro real montado en la PTZ | Barrido suave antes del montaje mecánico |
| Corriente real consumida | Medir con fuente limitada en corriente (contrastar con los 5–7 A de ficha) |
| Torque/peso/IP exactos | Discrepancias menores entre tabla (40/45/50 kg, 95 g, IP67) y ficha X50 Pro (42/46/50 kg, 88 g, IPX8) — irrelevantes para el diseño |

---

## Fuentes

- **Ficha oficial del listado de compra** (AliExpress ID 1005009825461352, imágenes GXservo/Yipinservo aportadas por el usuario) — fuente primaria de la v2.
- https://www.integy.com/st_prod.html?p_prodid=45118&p_catid=98 (Integy — GX3350ProBLS X50)
- http://www.gxservo.com/en/ (fabricante: Dongguan Gongxun Power Technology)
- https://h2-rd.com/products/gx-servo-usb-servo-programmer (programador USB GX/Yipin)
- https://raw.githubusercontent.com/arduino-libraries/Servo/master/src/Servo.h (librería Servo)
- https://github.com/arduino/Arduino/issues/3860 (bug detach/PWM 9-10)
- https://learn.adafruit.com/adafruit-arduino-lesson-14-servo-motors/if-the-servo-misbehaves (condensadores y síntomas)
- https://www.kpower.com/insight_bldc/7445.html (dimensionado de BEC)
- Investigación v1 (comparables DFRobot 35 kg y ANNIMOS DS5160, precedente GX3270BLS 180/270/360°): ver historial del proyecto.
