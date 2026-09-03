// Autotest de la placa para el proyecto ufomount (PTZ).
// Parpadea el LED integrado y responde al handshake "PING" por serie.

const uint8_t LED = LED_BUILTIN;

void setup() {
  pinMode(LED, OUTPUT);
  Serial.begin(115200);
  Serial.println("UFOMOUNT SELFTEST LISTO");
}

void loop() {
  static unsigned long last = 0;
  if (millis() - last >= 500) {
    last = millis();
    digitalWrite(LED, !digitalRead(LED));
  }

  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();
    if (cmd == "PING") {
      Serial.println("PONG UNO OK");
    }
  }
}
