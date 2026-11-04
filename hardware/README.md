# Demo traffic signal (LEDs)

A tabletop 4-way signal that ResQRoute drives in real time. A camera or GPS ambulance turns its approach green, and it returns to the normal cycle once the ambulance has passed.

## Parts
- An Arduino Uno or Nano (USB serial), **or** an ESP32 dev board (Wi-Fi).
- 12 LEDs: 4 red, 4 amber, 4 green.
- 12 × 220–330 Ω resistors.
- A breadboard and jumper wires.

## Wiring

| Approach | Arduino R / Y / G | ESP32 R / Y / G |
|---|---|---|
| N | 2 / 3 / 4 | 13 / 12 / 14 |
| E | 5 / 6 / 7 | 27 / 26 / 25 |
| S | 8 / 9 / 10 | 33 / 32 / 15 |
| W | 11 / 12 / 13 | 4 / 16 / 17 |

Connect each pin → resistor → LED anode (long leg). Connect the LED cathode (short leg) → GND.

## Arduino (USB serial)
1. Open `arduino_traffic_light/arduino_traffic_light.ino` in the Arduino IDE and upload it.
2. Close the IDE's Serial Monitor, because only one program can use the port.
3. On the dashboard, go to **Signals → Edit junction → Output: Arduino / ESP32 over USB serial**.
   - Choose the port: `COM3` on Windows, `/dev/ttyUSB0` or `/dev/ttyACM0` on Linux, `/dev/cu.usbmodem…` on macOS.
   - Set the baud rate to 115200.
4. Save. The LEDs follow the dashboard.

Protocol: each line looks like `S N=G E=R S=G W=R M=PRIORITY`. The board replies `OK`, or `ERR conflict`.

## ESP32 (Wi-Fi)
1. Install the **ArduinoJson** library.
2. Set `WIFI_SSID` and `WIFI_PASS` in `esp32_traffic_light/esp32_traffic_light.ino` and upload it.
3. Read the IP address from the Serial Monitor.
4. On the dashboard, choose **Output: HTTP device** with URL `http://<ip>/state`.
5. Opening `http://<ip>/state` in a browser shows the current lamps.

## Safety behaviour (both boards)
- **Conflict monitor:** if a command would make crossing approaches green or amber together, the board refuses it and flashes amber.
- **Watchdog:** if no command arrives for 5 s, the board flashes amber. The backend sends a heartbeat every 2 s, so this only happens if the PC or the link fails.

These boards are for demonstration only. Never connect them to real traffic signals.
