/*
  ResQRoute AI – 4-approach LED traffic-light model driven over USB serial.

  The backend's "serial" driver sends one line per state change, e.g.
      S N=G E=R S=G W=R M=PRIORITY
  (G = green, Y = amber, R = red, X = dark). Baud rate 115200.

  Safety, as on real controllers:
   * conflict monitor: if two crossing approaches (N/S vs E/W) would both be green or amber,
     the board ignores the command and flashes amber everywhere;
   * watchdog: no command for 5 s -> flashing amber (the PC or link failed).

  Wiring (each LED through a 220–330 ohm resistor to GND):
      approach  red  amber  green
      N          2     3      4
      E          5     6      7
      S          8     9     10
      W         11    12     13
*/

const int PINS[4][3] = { {2, 3, 4}, {5, 6, 7}, {8, 9, 10}, {11, 12, 13} };  // N, E, S, W : R, Y, G
const char NAMES[4] = { 'N', 'E', 'S', 'W' };
char state[4] = { 'R', 'R', 'R', 'R' };
unsigned long lastCommand = 0;
bool failsafe = true;
String line;

void show() {
  for (int i = 0; i < 4; i++) {
    digitalWrite(PINS[i][0], state[i] == 'R');
    digitalWrite(PINS[i][1], state[i] == 'Y');
    digitalWrite(PINS[i][2], state[i] == 'G');
  }
}

bool active(char s) { return s == 'G' || s == 'Y'; }

bool conflicting(const char s[4]) {
  bool ns = active(s[0]) || active(s[2]);
  bool ew = active(s[1]) || active(s[3]);
  return ns && ew;
}

void handleLine(const String &l) {
  if (!l.startsWith("S ")) return;
  char next[4];
  for (int i = 0; i < 4; i++) next[i] = state[i];
  for (int i = 0; i < 4; i++) {
    String key = String(NAMES[i]) + "=";
    int p = l.indexOf(key);
    if (p >= 0 && p + 2 < (int)l.length()) next[i] = l.charAt(p + 2);
  }
  if (conflicting(next)) {        // never show crossing greens, whatever we were told
    failsafe = true;
    Serial.println("ERR conflict");
    return;
  }
  for (int i = 0; i < 4; i++) state[i] = next[i];
  failsafe = false;
  lastCommand = millis();
  show();
  Serial.println("OK");
}

void setup() {
  Serial.begin(115200);
  for (int i = 0; i < 4; i++)
    for (int j = 0; j < 3; j++) pinMode(PINS[i][j], OUTPUT);
  show();
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();
    if (c == '\n') { handleLine(line); line = ""; }
    else if (c != '\r' && line.length() < 80) line += c;
  }
  if (millis() - lastCommand > 5000) failsafe = true;
  if (failsafe) {                 // flashing amber
    bool on = (millis() / 500) % 2;
    for (int i = 0; i < 4; i++) {
      digitalWrite(PINS[i][0], LOW);
      digitalWrite(PINS[i][1], on);
      digitalWrite(PINS[i][2], LOW);
    }
  }
}
