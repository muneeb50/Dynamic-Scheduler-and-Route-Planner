/*
  ResQRoute AI – 4-approach LED traffic-light model on an ESP32 over Wi-Fi.

  Configure a junction with driver {"type": "http", "url": "http://<esp32-ip>/state"}.
  The backend POSTs JSON: {"junction":"J1","approaches":{"N":"G","E":"R","S":"G","W":"R"},"mode":"PRIORITY"}
  GET /state returns the current lamps (handy for testing in a browser).

  Same safety rules as the Arduino version: conflict monitor + 5 s watchdog -> flashing amber.
  Needs the ArduinoJson library (v7) – Arduino IDE: Tools > Manage Libraries > "ArduinoJson".

  Wiring (GPIO -> 220–330 ohm -> LED -> GND):
      approach  red  amber  green
      N          13    12     14
      E          27    26     25
      S          33    32     15
      W           4    16     17
*/
#include <WiFi.h>
#include <WebServer.h>
#include <ArduinoJson.h>

const char *WIFI_SSID = "YOUR_WIFI";
const char *WIFI_PASS = "YOUR_PASSWORD";

const int PINS[4][3] = { {13, 12, 14}, {27, 26, 25}, {33, 32, 15}, {4, 16, 17} };
const char *NAMES[4] = { "N", "E", "S", "W" };
char state[4] = { 'R', 'R', 'R', 'R' };
String mode = "BOOT";
unsigned long lastCommand = 0;
bool failsafe = true;
WebServer server(80);

void show() {
  for (int i = 0; i < 4; i++) {
    digitalWrite(PINS[i][0], state[i] == 'R');
    digitalWrite(PINS[i][1], state[i] == 'Y');
    digitalWrite(PINS[i][2], state[i] == 'G');
  }
}

bool active(char s) { return s == 'G' || s == 'Y'; }

void handleState() {
  if (server.method() == HTTP_GET) {
    String out = "{\"approaches\":{";
    for (int i = 0; i < 4; i++) {
      out += "\""; out += NAMES[i]; out += "\":\""; out += state[i]; out += "\"";
      if (i < 3) out += ",";
    }
    out += "},\"mode\":\"" + (failsafe ? String("FAILSAFE") : mode) + "\"}";
    server.send(200, "application/json", out);
    return;
  }
  JsonDocument doc;
  if (deserializeJson(doc, server.arg("plain"))) { server.send(400, "text/plain", "bad json"); return; }
  char next[4];
  for (int i = 0; i < 4; i++) {
    const char *v = doc["approaches"][NAMES[i]] | "";
    next[i] = (v[0] != '\0') ? v[0] : state[i];
  }
  bool ns = active(next[0]) || active(next[2]);
  bool ew = active(next[1]) || active(next[3]);
  if (ns && ew) { failsafe = true; server.send(409, "text/plain", "conflict"); return; }
  for (int i = 0; i < 4; i++) state[i] = next[i];
  mode = String((const char *)(doc["mode"] | "NORMAL"));
  failsafe = false;
  lastCommand = millis();
  show();
  server.send(200, "application/json", "{\"ok\":true}");
}

void setup() {
  Serial.begin(115200);
  for (int i = 0; i < 4; i++)
    for (int j = 0; j < 3; j++) pinMode(PINS[i][j], OUTPUT);
  show();
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  while (WiFi.status() != WL_CONNECTED) { delay(300); Serial.print("."); }
  Serial.print("\nResQRoute signal ready at http://"); Serial.print(WiFi.localIP()); Serial.println("/state");
  server.on("/state", handleState);
  server.begin();
}

void loop() {
  server.handleClient();
  if (millis() - lastCommand > 5000) failsafe = true;
  if (failsafe) {
    bool on = (millis() / 500) % 2;
    for (int i = 0; i < 4; i++) {
      digitalWrite(PINS[i][0], LOW);
      digitalWrite(PINS[i][1], on);
      digitalWrite(PINS[i][2], LOW);
    }
  }
}
