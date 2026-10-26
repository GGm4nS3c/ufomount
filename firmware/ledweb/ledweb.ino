// Prueba de la shield Ethernet: pagina web para prender y apagar el LED
// interno (L, D13) del UNO. Abrir http://192.168.200.50/ en el navegador.
// Por el USB (115200) tambien acepta '1' (prender) y '0' (apagar), para
// probar el LED aunque la red no ande.
//
// Ojo: en el UNO el LED interno comparte el pin D13 con el reloj SPI (SCK)
// de la shield. Mientras el SPI esta activo el pin lo maneja el SPI y
// digitalWrite no tiene efecto, por eso se apaga el SPI (bit SPE) despues de
// cada acceso a la red. La libreria lo vuelve a activar sola en el siguiente
// acceso (beginTransaction), y el W5100 ignora SCK mientras su CS esta alto.
//
// Si la libreria no detecta el chip, entra en modo forzado: le escribe la
// configuracion de red a ciegas (ver forceW5100).
#include <SPI.h>
#include <Ethernet.h>

byte MAC[] = {0x02, 0x55, 0x46, 0x4F, 0x00, 0x01};
const IPAddress NET_IP(192, 168, 200, 50);
const IPAddress NET_GW(192, 168, 200, 254);
const IPAddress NET_MASK(255, 255, 255, 0);
const uint8_t PIN_SD_CS = 4;
const unsigned long POLL_MS = 20;  // poco SPI = el LED no se ve atenuado

EthernetServer server(80);
bool netOk = false;
bool ledOn = false;

const char PAGE_HEAD[] PROGMEM =
  "HTTP/1.1 200 OK\r\n"
  "Content-Type: text/html; charset=utf-8\r\n"
  "Cache-Control: no-store\r\n"
  "Connection: close\r\n\r\n"
  "<!doctype html><html><head><meta charset=utf-8>"
  "<meta name=viewport content=\"width=device-width,initial-scale=1\">"
  "<title>LED UNO</title><style>"
  "body{font-family:sans-serif;text-align:center;background:#16181c;"
  "color:#eee;margin:40px 16px}"
  "a{display:inline-block;margin:8px;padding:16px 32px;border-radius:10px;"
  "color:#fff;text-decoration:none;font-size:20px}"
  ".on{background:#2e9d4a}.off{background:#b33a3a}"
  "</style></head><body><h1>LED interno del UNO</h1><p>Estado: <b>";
const char PAGE_TAIL[] PROGMEM =
  "</b></p><a class=on href=/on>Prender</a>"
  "<a class=off href=/off>Apagar</a></body></html>";
const char NOT_FOUND[] PROGMEM =
  "HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";

// Devuelve D13 al LED: apaga el SPI hasta el proximo acceso a la red.
void releaseSck() { SPCR &= ~_BV(SPE); }

// --- Modo forzado: configurar el W5100 escribiendo a ciegas ---
// La libreria no usa un chip que no contesta por MISO. Escribir solo usa
// MOSI, SCK y CS: si esa mitad llega, el W5100 queda con su IP y contesta el
// ping por si solo, sin que el UNO tenga que leer nada.
const SPISettings RAW_SPI(1000000, MSBFIRST, SPI_MODE0);
const uint8_t PIN_W5100_CS = 10;

void rawWrite(uint16_t addr, uint8_t value) {
  SPI.beginTransaction(RAW_SPI);
  digitalWrite(PIN_W5100_CS, LOW);
  SPI.transfer(0xF0);            // codigo de escritura del W5100
  SPI.transfer(addr >> 8);
  SPI.transfer(addr & 0xFF);
  SPI.transfer(value);
  digitalWrite(PIN_W5100_CS, HIGH);
  SPI.endTransaction();
}

void forceW5100() {
  SPI.begin();
  pinMode(PIN_W5100_CS, OUTPUT);
  digitalWrite(PIN_W5100_CS, HIGH);
  rawWrite(0x0000, 0x80);        // MR: reset por software
  delay(10);
  for (uint8_t i = 0; i < 4; i++) {
    rawWrite(0x0001 + i, NET_GW[i]);    // GAR
    rawWrite(0x0005 + i, NET_MASK[i]);  // SUBR
    rawWrite(0x000F + i, NET_IP[i]);    // SIPR
  }
  for (uint8_t i = 0; i < 6; i++) rawWrite(0x0009 + i, MAC[i]);  // SHAR
}

void setLed(bool on) {
  ledOn = on;
  digitalWrite(LED_BUILTIN, on ? HIGH : LOW);
  Serial.println(on ? F("LED prendido") : F("LED apagado"));
}

// Manda un texto de la flash en bloques: de a un byte, cada caracter
// saldria en su propio paquete TCP.
void sendP(EthernetClient &c, PGM_P p) {
  uint8_t buf[64];
  size_t n = strlen_P(p);
  while (n) {
    size_t k = n < sizeof(buf) ? n : sizeof(buf);
    memcpy_P(buf, p, k);
    c.write(buf, k);
    p += k;
    n -= k;
  }
}

void serveClient(EthernetClient &client) {
  // Guarda la primera linea ("GET /on HTTP/1.1") y descarta los encabezados
  // hasta la linea vacia que los cierra.
  char req[48];
  uint8_t len = 0;
  bool first = true;
  uint8_t newlines = 0;
  unsigned long t0 = millis();
  while (client.connected() && millis() - t0 < 1000) {
    if (!client.available()) continue;
    char c = client.read();
    if (first) {
      if (c == '\n') first = false;
      else if (c != '\r' && len < sizeof(req) - 1) req[len++] = c;
    }
    if (c == '\n') {
      if (++newlines == 2) break;
    } else if (c != '\r') {
      newlines = 0;
    }
  }
  req[len] = '\0';
  Serial.print(F("pedido: "));
  Serial.println(req);

  if (strncmp(req, "GET /on ", 8) == 0) {
    setLed(true);
  } else if (strncmp(req, "GET /off ", 9) == 0) {
    setLed(false);
  } else if (strncmp(req, "GET / ", 6) != 0) {
    sendP(client, NOT_FOUND);
    client.stop();
    return;
  }
  sendP(client, PAGE_HEAD);
  sendP(client, ledOn ? PSTR("PRENDIDO") : PSTR("APAGADO"));
  sendP(client, PAGE_TAIL);
  client.stop();
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_SD_CS, OUTPUT);
  digitalWrite(PIN_SD_CS, HIGH);
  Ethernet.begin(MAC, NET_IP, NET_GW, NET_GW, NET_MASK);
  netOk = Ethernet.hardwareStatus() != EthernetNoHardware;

  Serial.println(F("LEDWEB listo"));
  if (netOk) {
    server.begin();
    switch (Ethernet.hardwareStatus()) {
      case EthernetW5100: Serial.print(F("red: W5100")); break;
      case EthernetW5200: Serial.print(F("red: W5200")); break;
      case EthernetW5500: Serial.print(F("red: W5500")); break;
      default: Serial.print(F("red: ?")); break;
    }
    Serial.print(F(", abrir http://"));
    Serial.print(Ethernet.localIP());
    Serial.println('/');
  } else {
    forceW5100();
    Serial.println(F("red: la libreria no detecta la shield"));
    Serial.println(F("modo forzado: IP 192.168.200.50 escrita a ciegas en el W5100"));
    Serial.println(F("  si contesta el ping, el chip recibe ordenes y solo falla MISO"));
    Serial.println(F("  la pagina no puede andar: el UNO no puede leer los pedidos"));
  }
  Serial.println(F("USB: 1 = prender, 0 = apagar"));

  releaseSck();
  pinMode(LED_BUILTIN, OUTPUT);
  setLed(false);
}

void loop() {
  int c = Serial.read();
  if (c == '1') setLed(true);
  else if (c == '0') setLed(false);

  static unsigned long lastPoll = 0;
  if (netOk && millis() - lastPoll >= POLL_MS) {
    lastPoll = millis();
    EthernetClient client = server.available();
    if (client) serveClient(client);
    releaseSck();
  }
}
