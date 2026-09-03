// Firmware PTZ ufomount v3 (2 servos GXservo GX3350ProBLS "X50 Pro").
// Eje X (pan, 270 grados) en D9 - Eje Y (tilt, 180 grados) en D10.
//
// v3, correcciones de la revision adversarial:
//  - Slew con acumulador fraccional: velocidades reales desde 5 us/s
//    (antes el minimo efectivo era 50 us/s y el control fino era imposible).
//  - Validacion estricta de argumentos: texto basura ya no se convierte
//    en 0 -> barrido violento al minimo.
//  - Watchdog de enlace opcional (WDT <ms>): si hay movimiento y el host
//    calla, se detiene (evita que un jog huerfano llegue al tope).
//  - DETACH fija tgt=cur: un ATTACH posterior no reanuda un movimiento viejo.
//  - LIMX/LIMY re-acotan el objetivo vigente.
//  - Parser serie no bloqueante con buffer fijo (sin String, sin readStringUntil).
//
// Comandos (115200 baudios, terminados en \n, mayus/minus indistinto):
//   PING                  -> PONG PTZ
//   X <us>  / Y <us>      -> mueve el eje (reacopla solo si hace falta)
//   SX / SY / STOP        -> detiene X / Y / ambos (tgt = pos actual)
//   ATTACH / DETACH       -> acopla/desacopla ambos (en la pos actual)
//   ATTACHX / ATTACHY / DETACHX / DETACHY
//   LIMX <min> <max>      -> limites de pulso (idem LIMY)
//   RATE <us_s>           -> velocidad ambos ejes (RATEX/RATEY por eje)
//   IDLE <ms>             -> auto-detach tras inactividad (0 = nunca)
//   WDT <ms>              -> watchdog de enlace (0 = off, por defecto off)
//   STATUS                -> estado (parseable por la GUI)

#include <Servo.h>
#include <ctype.h>
#include <string.h>

const uint8_t PIN_X = 9;
const uint8_t PIN_Y = 10;

const uint16_t CENTER_US = 1500;
const uint16_t HARD_MIN_US = 500;   // rango completo confirmado del servo
const uint16_t HARD_MAX_US = 2500;

Servo servoX, servoY;

uint16_t minX = 500, maxX = 2500;
uint16_t minY = 500, maxY = 2500;

uint16_t curX = CENTER_US, curY = CENTER_US;   // pulso actual aplicado
uint16_t tgtX = CENTER_US, tgtY = CENTER_US;   // pulso objetivo
uint16_t rateX = 500, rateY = 500;             // velocidad de slew (us/s)
uint32_t accX = 0, accY = 0;                   // resto fraccional (mili-us)
uint32_t idleMs = 3000;                        // auto-detach (0 = nunca)
uint32_t wdtMs = 0;                            // watchdog de enlace (0 = off)
bool attX = false, attY = false;
unsigned long lastActX = 0, lastActY = 0;
unsigned long lastCmdMs = 0;                   // ultima linea serie recibida

char rxBuf[40];
uint8_t rxLen = 0;

void setup() {
  Serial.begin(115200);
  Serial.println(F("UFOMOUNT PTZ v3 LISTO"));
}

void moveToward(uint16_t &cur, uint16_t tgt, uint16_t step) {
  if (cur < tgt) cur = (tgt - cur > step) ? cur + step : tgt;
  else if (cur > tgt) cur = (cur - tgt > step) ? cur - step : tgt;
}

// Acopla en la posicion actual estimada: sin salto al energizar.
void attachXNow() {
  servoX.writeMicroseconds(curX);
  servoX.attach(PIN_X, HARD_MIN_US, HARD_MAX_US);
  attX = true;
  lastActX = millis();
}

void attachYNow() {
  servoY.writeMicroseconds(curY);
  servoY.attach(PIN_Y, HARD_MIN_US, HARD_MAX_US);
  attY = true;
  lastActY = millis();
}

void detachXNow() { tgtX = curX; servoX.detach(); attX = false; }
void detachYNow() { tgtY = curY; servoY.detach(); attY = false; }

// Numero decimal estricto: digitos, espacios alrededor, nada mas.
bool parseU16(const char *s, uint16_t &out) {
  while (*s == ' ') s++;
  if (!*s) return false;
  uint32_t v = 0;
  for (; *s && *s != ' '; s++) {
    if (!isdigit((unsigned char)*s)) return false;
    v = v * 10 + (uint8_t)(*s - '0');
    if (v > 60000) return false;
  }
  while (*s == ' ') s++;
  if (*s) return false;
  out = (uint16_t)v;
  return true;
}

bool parse2U16(const char *s, uint16_t &a, uint16_t &b) {
  while (*s == ' ') s++;
  if (!isdigit((unsigned char)*s)) return false;
  uint32_t v = 0;
  for (; *s && *s != ' '; s++) {
    if (!isdigit((unsigned char)*s)) return false;
    v = v * 10 + (uint8_t)(*s - '0');
    if (v > 60000) return false;
  }
  a = (uint16_t)v;
  return parseU16(s, b);
}

void printStatus() {
  Serial.print(F("attX=")); Serial.print(attX);
  Serial.print(F(" attY=")); Serial.print(attY);
  Serial.print(F(" X=")); Serial.print(curX); Serial.print('/'); Serial.print(tgtX);
  Serial.print(F(" [")); Serial.print(minX); Serial.print('-'); Serial.print(maxX); Serial.print(']');
  Serial.print(F(" Y=")); Serial.print(curY); Serial.print('/'); Serial.print(tgtY);
  Serial.print(F(" [")); Serial.print(minY); Serial.print('-'); Serial.print(maxY); Serial.print(']');
  Serial.print(F(" rateX=")); Serial.print(rateX);
  Serial.print(F(" rateY=")); Serial.print(rateY);
  Serial.print(F(" idle=")); Serial.println(idleMs);
}

void handleCommand(char *cmd) {
  for (char *p = cmd; *p; p++) *p = toupper((unsigned char)*p);
  uint16_t v, lo, hi;

  if (!strcmp(cmd, "PING")) {
    Serial.println(F("PONG PTZ"));
  } else if (cmd[0] == 'X' && cmd[1] == ' ') {
    if (parseU16(cmd + 2, v) && v >= HARD_MIN_US && v <= HARD_MAX_US) {
      if (!attX) attachXNow();
      tgtX = constrain(v, minX, maxX);
      lastActX = millis();
      Serial.print(F("OK X -> ")); Serial.println(tgtX);
    } else {
      Serial.println(F("ERR X: valor invalido (500-2500)"));
    }
  } else if (cmd[0] == 'Y' && cmd[1] == ' ') {
    if (parseU16(cmd + 2, v) && v >= HARD_MIN_US && v <= HARD_MAX_US) {
      if (!attY) attachYNow();
      tgtY = constrain(v, minY, maxY);
      lastActY = millis();
      Serial.print(F("OK Y -> ")); Serial.println(tgtY);
    } else {
      Serial.println(F("ERR Y: valor invalido (500-2500)"));
    }
  } else if (!strcmp(cmd, "SX")) {
    tgtX = curX;
    Serial.print(F("OK SX @")); Serial.println(curX);
  } else if (!strcmp(cmd, "SY")) {
    tgtY = curY;
    Serial.print(F("OK SY @")); Serial.println(curY);
  } else if (!strcmp(cmd, "STOP")) {
    tgtX = curX; tgtY = curY;
    Serial.print(F("OK STOP @")); Serial.print(curX); Serial.print(','); Serial.println(curY);
  } else if (!strcmp(cmd, "ATTACH")) {
    attachXNow();
    delay(300);   // escalona el pico de energizado entre servos
    attachYNow();
    Serial.println(F("OK ATTACH X+Y"));
  } else if (!strcmp(cmd, "ATTACHX")) {
    attachXNow();
    Serial.println(F("OK ATTACHX"));
  } else if (!strcmp(cmd, "ATTACHY")) {
    attachYNow();
    Serial.println(F("OK ATTACHY"));
  } else if (!strcmp(cmd, "DETACH")) {
    detachXNow(); detachYNow();
    Serial.println(F("OK DETACH X+Y"));
  } else if (!strcmp(cmd, "DETACHX")) {
    detachXNow();
    Serial.println(F("OK DETACHX"));
  } else if (!strcmp(cmd, "DETACHY")) {
    detachYNow();
    Serial.println(F("OK DETACHY"));
  } else if (!strncmp(cmd, "LIMX ", 5) || !strncmp(cmd, "LIMY ", 5)) {
    if (parse2U16(cmd + 5, lo, hi) && lo < hi &&
        lo >= HARD_MIN_US && hi <= HARD_MAX_US) {
      if (cmd[3] == 'X') {
        minX = lo; maxX = hi;
        tgtX = constrain(tgtX, minX, maxX);
      } else {
        minY = lo; maxY = hi;
        tgtY = constrain(tgtY, minY, maxY);
      }
      Serial.print(F("OK LIM ")); Serial.print(lo); Serial.print(' '); Serial.println(hi);
    } else {
      Serial.println(F("ERR uso: LIMX <min> <max> dentro de 500-2500"));
    }
  } else if (!strncmp(cmd, "RATEX ", 6)) {
    if (parseU16(cmd + 6, v)) {
      rateX = constrain(v, 5, 5000);
      Serial.print(F("OK RATEX ")); Serial.println(rateX);
    } else Serial.println(F("ERR RATEX: valor invalido"));
  } else if (!strncmp(cmd, "RATEY ", 6)) {
    if (parseU16(cmd + 6, v)) {
      rateY = constrain(v, 5, 5000);
      Serial.print(F("OK RATEY ")); Serial.println(rateY);
    } else Serial.println(F("ERR RATEY: valor invalido"));
  } else if (!strncmp(cmd, "RATE ", 5)) {
    if (parseU16(cmd + 5, v)) {
      rateX = rateY = constrain(v, 5, 5000);
      Serial.print(F("OK RATE ")); Serial.println(rateX);
    } else Serial.println(F("ERR RATE: valor invalido"));
  } else if (!strncmp(cmd, "IDLE ", 5)) {
    if (parseU16(cmd + 5, v)) {
      idleMs = v;
      Serial.print(F("OK IDLE ")); Serial.println(idleMs);
    } else Serial.println(F("ERR IDLE: valor invalido"));
  } else if (!strncmp(cmd, "WDT ", 4)) {
    if (parseU16(cmd + 4, v)) {
      wdtMs = v;
      Serial.print(F("OK WDT ")); Serial.println(wdtMs);
    } else Serial.println(F("ERR WDT: valor invalido"));
  } else if (!strcmp(cmd, "STATUS")) {
    printStatus();
  } else if (cmd[0]) {
    Serial.println(F("ERR comando desconocido"));
  }
}

void slewAxis(bool att, uint16_t &cur, uint16_t tgt, uint16_t rate,
              uint32_t &acc, Servo &sv, unsigned long &lastAct,
              unsigned long now) {
  if (att && cur != tgt) {
    acc += (uint32_t)rate * 20;          // mili-us por tick de 20 ms
    uint16_t step = acc / 1000;
    if (step) {
      acc -= (uint32_t)step * 1000;
      moveToward(cur, tgt, step);
      sv.writeMicroseconds(cur);
    }
    lastAct = now;
  } else {
    acc = 0;
  }
}

void loop() {
  static unsigned long lastTick = 0;
  unsigned long now = millis();

  // lectura serie no bloqueante
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n' || c == '\r') {
      if (rxLen) {
        rxBuf[rxLen] = 0;
        rxLen = 0;
        lastCmdMs = now;
        handleCommand(rxBuf);
      }
    } else if (rxLen < sizeof(rxBuf) - 1) {
      rxBuf[rxLen++] = c;
    } else {
      rxLen = 0;   // linea demasiado larga: descartar
    }
  }

  if (now - lastTick >= 20) {
    lastTick = now;
    slewAxis(attX, curX, tgtX, rateX, accX, servoX, lastActX, now);
    slewAxis(attY, curY, tgtY, rateY, accY, servoY, lastActY, now);

    // Watchdog de enlace: movimiento en curso + host callado -> parar.
    bool moving = (attX && curX != tgtX) || (attY && curY != tgtY);
    if (wdtMs > 0 && moving && now - lastCmdMs >= wdtMs) {
      tgtX = curX; tgtY = curY;
      Serial.println(F("WATCHDOG STOP"));
    }

    // Auto-detach por inactividad: silencio y cero zumbido en reposo.
    if (idleMs > 0) {
      if (attX && curX == tgtX && now - lastActX >= idleMs) {
        detachXNow();
        Serial.println(F("AUTO DETACHX"));
      }
      if (attY && curY == tgtY && now - lastActY >= idleMs) {
        detachYNow();
        Serial.println(F("AUTO DETACHY"));
      }
    }
  }
}
