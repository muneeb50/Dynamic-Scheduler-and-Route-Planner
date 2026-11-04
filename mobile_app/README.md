# ResQRoute patient app (Flutter)

Request an ambulance by tapping a pickup point and choosing a hospital, then track it live (WebSocket with automatic reconnect, REST fallback and a stale-data warning).

## Run

```bash
cd mobile_app
flutter create --platforms=android,web --project-name resqroute_patient --org com.resqroute .   # generates the missing platform folders, keeps lib/ and the Android manifest
flutter pub get
```

Then, with the backend running on your laptop (`bash scripts/run.sh`):

| Where the app runs | Command |
|---|---|
| Android emulator | `flutter run` (defaults to `http://10.0.2.2:8000`, the emulator's alias for your laptop) |
| Chrome | `flutter run -d chrome --dart-define=API_BASE=http://localhost:8000` |
| Real phone on the same Wi-Fi | `flutter run --dart-define=API_BASE=http://<laptop-LAN-IP>:8000` (find the IP with `ipconfig` / `ifconfig`; allow port 8000 through the firewall) |

The included `android/app/src/main/AndroidManifest.xml` adds the INTERNET permission and allows plain HTTP to the local backend. Use HTTPS and remove `usesCleartextTraffic` for anything beyond a local demo.

`flutter test` runs the model tests.

## Structure

```
lib/
  main.dart                    app + theme
  config.dart                  API_BASE (dart-define), stale threshold
  models/models.dart           ServiceArea, Hospital, Booking, JourneyState …
  services/api_service.dart    REST calls with timeouts and readable errors
  services/tracking_service.dart  WebSocket tracking, back-off reconnect, polling fallback, stale detection
  screens/booking_screen.dart  map pickup, hospital, form validation
  screens/tracking_screen.dart live map, ETA, status, cancel
  widgets/map_widgets.dart     OSM tiles, attribution, markers
```

Pickups must be inside the simulated area (the app centres the map on it). GPS location is deliberately not used: your real position is outside the simulated network.
