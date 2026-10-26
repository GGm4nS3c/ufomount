// Firmware PTZ ufomount v4 (2 servos GXservo GX3350ProBLS "X50 Pro").
// Eje X (pan, 270 grados) en D2 - Eje Y (tilt, 180 grados) en D3.
//
// v4, control por red con shield Ethernet (W5100/W5200/W5500):
//  - Servos en D2 (X) y D3 (Y): la shield usa D10 como chip-select del
//    controlador de red (D11-D13 son el SPI y D4 la SD), y D8/D9 quedaban
//    junto a D10, en el extremo del conector que peor calza con la shield.
//  - Servidor TCP en 192.168.200.50:5000 con el mismo protocolo de lineas
//    que el puerto serie; USB y red funcionan a la vez.
//  - Una conexion TCP a la vez: una nueva reemplaza a la anterior, asi un
//    socket colgado nunca deja la montura sin control.
//  - Cada respuesta vuelve por donde llego el comando; los avisos
//    (AUTO DETACH, WATCHDOG) salen por ambos canales.
//  - El watchdog vigila al canal que ordeno el ultimo movimiento: los polls
//    STATUS del otro canal no mantienen vivo un jog huerfano.
//  - Si se cae la conexion TCP que movia la montura -> STOP.
//  - Sin shield (o sin chip reconocido) sigue funcionando solo por USB.
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
// Comandos (115200 baudios por USB o TCP puerto 5000, terminados en \n,
// mayus/minus indistinto):
//   PING                  -> PONG PTZ
//   X <us>  / Y <us>      -> mueve el eje (reacopla solo si hace falta)
//   SX / SY / STOP        -> detiene X / Y / ambos (tgt = pos actual)
//   ATTACH / DETACH       -> acopla/desacopla ambos (en la pos actual)
//   ATTACHX / ATTACHY / DETACHX / DETACHY
//   LIMX <min> <max>      -> limites de pulso (idem LIMY; Y arranca en 944-1833)
//   RATE <us_s>           -> velocidad ambos ejes (RATEX/RATEY por eje)
//   IDLE <ms>             -> auto-detach tras inactividad (0 = nunca)
//   WDT <ms>              -> watchdog de enlace (0 = off, por defecto off)
//   STATUS                -> estado (parseable por la GUI)
//   NET                   -> chip de red, enlace, IP y cliente TCP

#include <Servo.h>
#include <SPI.h>
#include <Ethernet.h>
#include <ctype.h>
#include <string.h>

const uint8_t PIN_X = 2;
const uint8_t PIN_Y = 3;
const uint8_t PIN_SD_CS = 4;   // SD de la shield: se deja deseleccionada

// Red (IP fija). MAC administrada localmente (bit 0x02): no choca con
// ningun fabricante; cambiar el ultimo byte si hay dos monturas en la red.
byte MAC[] = {0x02, 0x55, 0x46, 0x4F, 0x00, 0x01};
const IPAddress NET_IP(192, 168, 200, 50);
const IPAddress NET_GW(192, 168, 200, 254);
const IPAddress NET_MASK(255, 255, 255, 0);
const uint16_t TCP_PORT = 5000;

const uint16_t CENTER_US = 1500;
const uint16_t HARD_MIN_US = 500;   // rango completo confirmado del servo
const uint16_t HARD_MAX_US = 2500;

enum { SRC_SERIAL = 0, SRC_TCP = 1 };

Servo servoX, servoY;

uint16_t minX = 500, maxX = 2500;
// Tilt por defecto de -50 grados (suelo) a +30 (cielo), 0 = horizonte en
// 1500 us: mas alla la camara topa. La GUI puede cambiarlo con LIMY.
uint16_t minY = 944, maxY = 1833;

uint16_t curX = CENTER_US, curY = CENTER_US;   // pulso actual aplicado
uint16_t tgtX = CENTER_US, tgtY = CENTER_US;   // pulso objetivo
uint16_t rateX = 500, rateY = 500;             // velocidad de slew (us/s)
uint32_t accX = 0, accY = 0;                   // resto fraccional (mili-us)
uint32_t idleMs = 3000;                        // auto-detach (0 = nunca)
uint32_t wdtMs = 0;                            // watchdog de enlace (0 = off)
bool attX = false, attY = false;
unsigned long lastActX = 0, lastActY = 0;
unsigned long lastCmdMs[2] = {0, 0};           // ultima linea por canal
uint8_t moveSrc = SRC_SERIAL;                  // canal del ultimo movimiento

struct LineBuf {
  char buf[40];
  uint8_t len;
};
LineBuf rxSerial = {{0}, 0};
LineBuf rxTcp = {{0}, 0};

EthernetServer server(TCP_PORT);
EthernetClient client;                         // conexion TCP vigente
bool netOk = false;

// Junta la salida hacia el cliente TCP y la manda de a una linea por
// paquete (sin esto cada print() seria un segmento TCP aparte).
class TcpOut : public Print {
 public:
  size_t write(uint8_t c) override {
    buf[len++] = c;
    if (c == '\n' || len == sizeof(buf)) send();
    return 1;
  }
  void send() {
    if (len && client.connected()) client.write((const uint8_t *)buf, len);
    len = 0;
  }
 private:
  char buf[100];   // cabe una linea STATUS entera
  uint8_t len = 0;
};
TcpOut tcpOut;

void setup() {
  Serial.begin(115200);

  pinMode(PIN_SD_CS, OUTPUT);
  digitalWrite(PIN_SD_CS, HIGH);   // que la SD no conteste en el bus SPI
  Ethernet.begin(MAC, NET_IP, NET_GW, NET_GW, NET_MASK);   // dns = gateway
  netOk = Ethernet.hardwareStatus() != EthernetNoHardware;
  if (netOk) {
    // LAN: reintentos cortos, asi un cliente muerto no congela el loop
    // ~50 s esperando ACKs con los valores por defecto.
    Ethernet.setRetransmissionTimeout(100);
    Ethernet.setRetransmissionCount(3);
    server.begin();
  }

  Serial.println(F("UFOMOUNT PTZ v4 LISTO"));
  printNet(Serial);
}

void moveToward(uint16_t &cur, uint16_t tgt, uint16_t step) {
  if (cur < tgt) cur = (tgt - cur > step) ? cur + step : tgt;
  else if (cur > tgt) cur = (cur - tgt > step) ? cur - step : tgt;
}

bool isMoving() {
  return (attX && curX != tgtX) || (attY && curY != tgtY);
}

// Aviso asincrono: sale por USB y por la red.
void notify(const __FlashStringHelper *msg) {
  Serial.println(msg);
  tcpOut.println(msg);
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

void printStatus(Print &out) {
  out.print(F("attX=")); out.print(attX);
  out.print(F(" attY=")); out.print(attY);
  out.print(F(" X=")); out.print(curX); out.print('/'); out.print(tgtX);
  out.print(F(" [")); out.print(minX); out.print('-'); out.print(maxX); out.print(']');
  out.print(F(" Y=")); out.print(curY); out.print('/'); out.print(tgtY);
  out.print(F(" [")); out.print(minY); out.print('-'); out.print(maxY); out.print(']');
  out.print(F(" rateX=")); out.print(rateX);
  out.print(F(" rateY=")); out.print(rateY);
  out.print(F(" idle=")); out.println(idleMs);
}

void printNet(Print &out) {
  out.print(F("NET hw="));
  switch (Ethernet.hardwareStatus()) {
    case EthernetW5100: out.print(F("W5100")); break;
    case EthernetW5200: out.print(F("W5200")); break;
    case EthernetW5500: out.print(F("W5500")); break;
    default: out.println(F("ninguno (solo USB)")); return;
  }
  out.print(F(" link="));
  switch (Ethernet.linkStatus()) {
    case LinkON: out.print(F("on")); break;
    case LinkOFF: out.print(F("off")); break;
    default: out.print('?'); break;   // la W5100 no informa el enlace
  }
  out.print(F(" ip=")); out.print(Ethernet.localIP());
  out.print(':'); out.print(TCP_PORT);
  out.print(F(" cliente=")); out.println(client.connected() ? 1 : 0);
}

void handleCommand(char *cmd, Print &out, uint8_t src) {
  for (char *p = cmd; *p; p++) *p = toupper((unsigned char)*p);
  uint16_t v, lo, hi;

  if (!strcmp(cmd, "PING")) {
    out.println(F("PONG PTZ"));
  } else if (cmd[0] == 'X' && cmd[1] == ' ') {
    if (parseU16(cmd + 2, v) && v >= HARD_MIN_US && v <= HARD_MAX_US) {
      if (!attX) attachXNow();
      tgtX = constrain(v, minX, maxX);
      lastActX = millis();
      moveSrc = src;
      out.print(F("OK X -> ")); out.println(tgtX);
    } else {
      out.println(F("ERR X: valor invalido (500-2500)"));
    }
  } else if (cmd[0] == 'Y' && cmd[1] == ' ') {
    if (parseU16(cmd + 2, v) && v >= HARD_MIN_US && v <= HARD_MAX_US) {
      if (!attY) attachYNow();
      tgtY = constrain(v, minY, maxY);
      lastActY = millis();
      moveSrc = src;
      out.print(F("OK Y -> ")); out.println(tgtY);
    } else {
      out.println(F("ERR Y: valor invalido (500-2500)"));
    }
  } else if (!strcmp(cmd, "SX")) {
    tgtX = curX;
    out.print(F("OK SX @")); out.println(curX);
  } else if (!strcmp(cmd, "SY")) {
    tgtY = curY;
    out.print(F("OK SY @")); out.println(curY);
  } else if (!strcmp(cmd, "STOP")) {
    tgtX = curX; tgtY = curY;
    out.print(F("OK STOP @")); out.print(curX); out.print(','); out.println(curY);
  } else if (!strcmp(cmd, "ATTACH")) {
    attachXNow();
    delay(300);   // escalona el pico de energizado entre servos
    attachYNow();
    out.println(F("OK ATTACH X+Y"));
  } else if (!strcmp(cmd, "ATTACHX")) {
    attachXNow();
    out.println(F("OK ATTACHX"));
  } else if (!strcmp(cmd, "ATTACHY")) {
    attachYNow();
    out.println(F("OK ATTACHY"));
  } else if (!strcmp(cmd, "DETACH")) {
    detachXNow(); detachYNow();
    out.println(F("OK DETACH X+Y"));
  } else if (!strcmp(cmd, "DETACHX")) {
    detachXNow();
    out.println(F("OK DETACHX"));
  } else if (!strcmp(cmd, "DETACHY")) {
    detachYNow();
    out.println(F("OK DETACHY"));
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
      out.print(F("OK LIM ")); out.print(lo); out.print(' '); out.println(hi);
    } else {
      out.println(F("ERR uso: LIMX <min> <max> dentro de 500-2500"));
    }
  } else if (!strncmp(cmd, "RATEX ", 6)) {
    if (parseU16(cmd + 6, v)) {
      rateX = constrain(v, 5, 5000);
      out.print(F("OK RATEX ")); out.println(rateX);
    } else out.println(F("ERR RATEX: valor invalido"));
  } else if (!strncmp(cmd, "RATEY ", 6)) {
    if (parseU16(cmd + 6, v)) {
      rateY = constrain(v, 5, 5000);
      out.print(F("OK RATEY ")); out.println(rateY);
    } else out.println(F("ERR RATEY: valor invalido"));
  } else if (!strncmp(cmd, "RATE ", 5)) {
    if (parseU16(cmd + 5, v)) {
      rateX = rateY = constrain(v, 5, 5000);
      out.print(F("OK RATE ")); out.println(rateX);
    } else out.println(F("ERR RATE: valor invalido"));
  } else if (!strncmp(cmd, "IDLE ", 5)) {
    if (parseU16(cmd + 5, v)) {
      idleMs = v;
      out.print(F("OK IDLE ")); out.println(idleMs);
    } else out.println(F("ERR IDLE: valor invalido"));
  } else if (!strncmp(cmd, "WDT ", 4)) {
    if (parseU16(cmd + 4, v)) {
      wdtMs = v;
      out.print(F("OK WDT ")); out.println(wdtMs);
    } else out.println(F("ERR WDT: valor invalido"));
  } else if (!strcmp(cmd, "STATUS")) {
    printStatus(out);
  } else if (!strcmp(cmd, "NET")) {
    printNet(out);
  } else if (cmd[0]) {
    out.println(F("ERR comando desconocido"));
  }
}

// Acumula un caracter del canal; al cerrar la linea ejecuta el comando.
void feed(LineBuf &lb, char c, Print &out, uint8_t src, unsigned long now) {
  if (c == '\n' || c == '\r') {
    if (lb.len) {
      lb.buf[lb.len] = 0;
      lb.len = 0;
      lastCmdMs[src] = now;
      handleCommand(lb.buf, out, src);
    }
  } else if ((uint8_t)c < 0x20 || (uint8_t)c >= 0x7F) {
    // bytes de control (p. ej. negociacion telnet): se ignoran
  } else if (lb.len < sizeof(lb.buf) - 1) {
    lb.buf[lb.len++] = c;
  } else {
    lb.len = 0;   // linea demasiado larga: descartar
  }
}

// Suelta la conexion TCP; si era la que movia la montura, la detiene.
void dropClient() {
  tcpOut.send();
  client.stop();
  rxTcp.len = 0;
  if (isMoving() && moveSrc == SRC_TCP) {
    tgtX = curX; tgtY = curY;
    Serial.println(F("NET cliente caido con movimiento -> STOP"));
  }
}

void serviceNet(unsigned long now) {
  if (!netOk) return;

  EthernetClient nc = server.accept();
  if (nc) {
    if (client.connected()) {
      tcpOut.println(F("BYE otra conexion tomo el control"));
      dropClient();
    }
    nc.setConnectionTimeout(200);   // stop() no bloquea 1 s con un peer muerto
    client = nc;
    rxTcp.len = 0;
    lastCmdMs[SRC_TCP] = now;
    tcpOut.println(F("UFOMOUNT PTZ v4 LISTO (TCP)"));
    Serial.println(F("NET cliente conectado"));
  }

  if (!client) return;
  if (!client.connected()) {
    dropClient();
    Serial.println(F("NET cliente desconectado"));
    return;
  }
  uint8_t chunk[16];
  int n;
  while ((n = client.read(chunk, sizeof(chunk))) > 0) {
    for (int i = 0; i < n; i++) feed(rxTcp, chunk[i], tcpOut, SRC_TCP, now);
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

  // lectura no bloqueante de ambos canales
  while (Serial.available()) {
    feed(rxSerial, Serial.read(), Serial, SRC_SERIAL, now);
  }
  serviceNet(now);

  if (now - lastTick >= 20) {
    lastTick = now;
    slewAxis(attX, curX, tgtX, rateX, accX, servoX, lastActX, now);
    slewAxis(attY, curY, tgtY, rateY, accY, servoY, lastActY, now);

    // Watchdog de enlace: movimiento en curso + su canal callado -> parar.
    if (wdtMs > 0 && isMoving() && now - lastCmdMs[moveSrc] >= wdtMs) {
      tgtX = curX; tgtY = curY;
      notify(F("WATCHDOG STOP"));
    }

    // Auto-detach por inactividad: silencio y cero zumbido en reposo.
    if (idleMs > 0) {
      if (attX && curX == tgtX && now - lastActX >= idleMs) {
        detachXNow();
        notify(F("AUTO DETACHX"));
      }
      if (attY && curY == tgtY && now - lastActY >= idleMs) {
        detachYNow();
        notify(F("AUTO DETACHY"));
      }
    }
  }
}
