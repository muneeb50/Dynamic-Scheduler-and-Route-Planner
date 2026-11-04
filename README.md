# ResQRoute AI

**Intelligent ambulance dispatch, live tracking, emergency traffic-signal prioritization and adaptive traffic recovery** – a complete, runnable simulation-based prototype for Nerul–Seawoods (Navi Mumbai), scalable to the whole city.

Real OpenStreetMap roads + simulated traffic (thousands of vehicles) + simulated signals with route-aware emergency priority + adaptive recovery after the ambulance passes + YOLO/OpenCV cameras that recognise ambulances. No real signals are touched and no live signal data is needed.

| Part | Technology | Folder |
|---|---|---|
| Patient app | Flutter (flutter_map, WebSocket tracking) | `mobile_app/` |
| Backend | Python, FastAPI, SQLAlchemy, WebSockets | `backend/` |
| Database | SQLite (default) or PostgreSQL + PostGIS | `docker-compose.yml` |
| Real maps | **OpenStreetMap via the Overpass API** (place search: Nominatim): presets from Nerul–Seawoods to all of Navi Mumbai, any searched place or visible map area, or an uploaded `.osm` export; junction consolidation, roundabouts, signal mapping + inference; cached for offline use | `backend/app/mapdata/`, `data/maps/` |
| Traffic simulation | Built-in IDM simulator (default; city scale – demand scales with the map, ~8,000 vehicles on a 14×12 km network) or SUMO via TraCI | `backend/app/sim/` |
| Simulated signals, priority & recovery | Every signal of the map simulated (fixed-time, safety-checked); green for an approaching ambulance (booking, test ambulance, **camera**, operator), bounded adaptive recovery after it passes | `backend/app/traffic_control/`, `backend/app/recovery/` |
| Video analytics (ML) | **YOLO11** (Ultralytics), **YOLOE** open-vocabulary (ambulance, auto-rickshaw) and YOLO via **OpenCV DNN**, plus OpenCV motion detection; tracking, counting, queues, congestion – on uploaded videos, RTSP/IP cameras, webcams and the browser camera, streamed live | `computer_vision/video/` |
| Ambulance recognition | **Trained vehicle classifier** (YOLO11n-cls on ~7,500 Indian traffic/ambulance crops + 3,600 hard negatives: ambulance, fire/police, auto-rickshaw, bus, motorcycle, car) + offline OCR of "AMBULANCE" + flashing/lit beacon + livery, fused by a fitted logistic model, with temporal voting in video; zone / stop-line events. Crowded photos use YOLO11m at two scales + tiling. See [docs/ambulance_model.md](docs/ambulance_model.md) | `computer_vision/video/ambulance.py`, `vehicle_classifier.py`, `computer_vision/train/` |
| LED / external signals (optional) | Stand-alone wall-clock junctions for an **Arduino / ESP32 LED signal** (HTTP/MQTT too), optional read-only SPaT / SensorThings / MQTT mirrors; camera- and GPS-triggered corridors | `backend/app/field/`, `hardware/` |
| Simulated cameras | OpenCV detection + queue estimation on rendered views of the simulation, scored against ground truth | `computer_vision/` |
| Admin dashboard | React + TypeScript + Leaflet + Recharts | `admin_dashboard/` |

## Quick start (about 5 minutes)

Requirements: **Python 3.10+**. Node 18+ is only needed to rebuild the dashboard (a built copy is included). Flutter is only needed for the mobile app.

**macOS / Linux**
```bash
bash scripts/setup.sh        # venv + Python packages (+ dashboard build if npm is installed)
bash scripts/run.sh          # http://localhost:8000
```

**Windows (PowerShell)**
```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
powershell -ExecutionPolicy Bypass -File scripts\run.ps1     # http://localhost:8000
```

Open <http://localhost:8000>: the control room starts with live traffic. On the **first start it downloads the real Nerul–Seawoods roads from OpenStreetMap** (internet needed once, 10–60 s; the demo grid runs meanwhile) and switches to them automatically. Go to **Request ambulance**, tap a pickup point, request, and watch the ambulance get green lights on the **Live map**. API docs: <http://localhost:8000/docs>.

**Bigger area / more traffic:** *Live map → Map area* (presets up to all of Navi Mumbai, place search, "load visible area", `.osm` upload), *Demand* low → peak, and *Random emergencies* (N bookings per simulated hour). Details and benchmark: [docs/maps.md](docs/maps.md).

<details><summary>Manual steps (if you prefer)</summary>

```bash
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r backend/requirements.txt
cd backend && uvicorn app.main:app --reload               # API + built dashboard on :8000

# dashboard with hot reload (optional, second terminal)
cd admin_dashboard && npm install && npm run dev          # http://localhost:5173 (proxies to :8000)
```
</details>

### Video analytics (YOLO + OpenCV)
Open the **Video AI** tab → **+ Add** → upload a traffic video, paste a camera stream URL (e.g. a phone running an IP-camera app), use the laptop webcam or this browser's camera. Webcams show **every object** the model sees (people, phone, cup… in grey; vehicles in colour) so you can see detection working anywhere, and **Test detector** checks the model on a test photo. You get the annotated live video, live counts / queue / flow / congestion, and for uploads in *batch* mode an annotated video + CSV + JSON. **Photos** work too (+ Add → Photo, or 📸 from the browser camera): objects, vehicles by class, occupancy, congestion and an ambulance check per photo. A *Speed* setting trades accuracy for frame rate (480 px ≈ 2× faster than 640 px on a CPU; GPUs are used automatically), and cameras reconnect by themselves after drops. Command line: `python computer_vision/detect_video.py --source clip.mp4 --out annotated.mp4` or `--source 0 --show`. Details: [docs/video.md](docs/video.md).

**Camera ambulance → simulated signal turns green:**
1. On **Video AI**, open your webcam (*Server webcam* or *This browser's camera*), then under *Connect this camera to a signal* pick **Simulated junction (map)**, a junction and an approach, and **Attach**.
2. When an ambulance (or an ambulance video shown to the camera) is recognised in the zone, that approach of the simulated signal goes amber → all-red → green (watch it on **Signals** or the **Live map**). It is released when the ambulance crosses the stop line, then the junction recovers and returns to its normal cycle.
3. Without a camera: **Intersections → Send test ambulance through** (or *Green now*) on any junction. See [docs/signals.md](docs/signals.md). An optional Arduino/ESP32 LED model signal is under *Signals → advanced* ([hardware/README.md](hardware/README.md)).

The setup script installs YOLO (PyTorch CPU, a few hundred MB). To skip it: `SKIP_YOLO=1 bash scripts/setup.sh` – OpenCV DNN (bundled ONNX model) and motion detection still work.

### Patient app (Flutter)
See [mobile_app/README.md](mobile_app/README.md): `flutter create … .` once to generate platform folders, then `flutter run` (Android emulator) or `flutter run -d chrome --dart-define=API_BASE=http://localhost:8000`.

### Real roads (OpenStreetMap)
Downloaded automatically on first start and selectable in the dashboard. Command line:
```bash
python simulation/tools/fetch_osm.py --list              # presets and cached areas
python simulation/tools/fetch_osm.py --area navi-mumbai  # city scale (main roads)
python simulation/benchmark.py --demand medium high peak # how much traffic your machine handles on that map
```
Check the imported signals and hospitals; see [docs/maps.md](docs/maps.md) and [data/README.md](data/README.md).

### SUMO (optional)
```bash
pip install -r backend/requirements-sumo.txt   # bundles SUMO binaries
python simulation/tools/build_sumo_net.py      # network.json -> simulation/network/resq.net.xml
SIM_ENGINE=sumo bash scripts/run.sh            # Windows: $env:SIM_ENGINE="sumo"; scripts\run.ps1
python simulation/run_simulation.py trial --engine sumo --gui    # watch one trip in sumo-gui
```

### PostgreSQL + PostGIS (optional)
```bash
docker compose up -d db
pip install -r backend/requirements-postgres.txt
DATABASE_URL=postgresql+psycopg2://resq:resq@localhost:5432/resqroute bash scripts/run.sh
```
Geometry columns are created automatically ([docs/database.md](docs/database.md)).

## Evaluation

```bash
python simulation/run_simulation.py experiment --seeds 1 2 3 4 5 6 7 8 9 10    # matched baseline vs priority
python -m computer_vision.evaluation.evaluate --seconds 300                     # CV accuracy vs ground truth
python simulation/benchmark.py --minutes 20                                     # city-scale throughput on the current map
cd backend && pytest -q                                                         # 61 automated tests
```
Or use the **Analytics** and **Computer vision** tabs. Reference numbers (demo network): ambulance driving time −9.5 to −10.4% (−29 to −37 s, 95% CIs exclude zero), signal delay −61 to −85%, no measurable cost to other traffic, 0 conflicting greens in 60 runs – see [docs/results.md](docs/results.md).

## How it works

* **Real maps** – Overpass API download of roads, signals, hospitals and fire stations; dual-carriageway crossings and roundabouts consolidated into single junctions; OSM-tagged signals snapped to their junction plus inferred signals where main roads cross; the live simulation hot-swaps to a new area.
* **Dispatch & routing** – nearest ambulance by emergency-aware ETA: road graph + live simulated queues and spill-back.
* **Route-aware priority** – for each ambulance, look ahead *along its route* to the next signal; request the green when the ETA falls within *switching time (min green + amber + all-red) + queue-discharge time for vehicles ahead + margin*, so the queue is flushed before the ambulance arrives. Only GREEN→AMBER→ALL-RED→GREEN transitions exist, so conflicting greens are impossible, and every tick re-checks this. Priority is released only when the ambulance is observed past the junction (max-hold timeout as a safety net, logged).
* **Adaptive recovery** – baseline queues from the 90 s before the request; after release, each new green gets `clamp(base + 2 s × max backlog per lane, min, max)`; completes when every approach is within tolerance for 10 s, or logs a timeout with the residual backlog.
* **Ambulance recognition** – a vehicle classifier we trained on public Indian traffic and ambulance datasets, OCR of "AMBULANCE" (also mirrored), flashing red/blue beacon variation and livery, fused per vehicle and voted over time per track; `python -m computer_vision.evaluation.photo_eval` and `ambulance_eval` measure it ([docs/ambulance_model.md](docs/ambulance_model.md)).
* **Cameras on simulated signals** – a camera attached to an approach requests green when it recognises an ambulance in its zone and releases it after the stop line, through the same controller and recovery as route priority. Operators and test ambulances use the same path.
* **LED / external signals (optional)** – the same safety-checked state machine in wall-clock time for an Arduino/ESP32 model signal, GPS or camera triggered.
* **Video analytics** – YOLO11 / OpenCV DNN / MOG2 detection → IoU tracker → zone, counting line, stopped-vehicle queue, flow, speed, congestion and emergency alerts; live MJPEG + WebSocket stats; cameras can feed approach queues to the controller.
* **Simulated cameras** – virtual overhead cameras per approach → background subtraction → boxes → stopped/moving from consecutive frames → queue; conservative fusion of low-confidence estimates; switchable as the controller's queue source.

Details: [docs/maps.md](docs/maps.md) · [docs/architecture.md](docs/architecture.md) · [docs/flowcharts.md](docs/flowcharts.md) (Mermaid diagrams for the report) · [docs/api.md](docs/api.md) · [docs/database.md](docs/database.md) · [docs/testing.md](docs/testing.md) · [docs/demo_script.md](docs/demo_script.md)

## Repository layout

```
backend/            FastAPI app (api, core, mapdata, sim, traffic_control, recovery, analytics, field, services) + tests
simulation/         tools (OSM fetch/import, synthetic test city, demo network, SUMO build), run_simulation.py, benchmark.py
computer_vision/    video/ (YOLO + OpenCV real-video pipeline), models/ (yolo11n .pt + .onnx), train/, tools/,
                    detect_video.py (CLI), rendering/detection/estimation/evaluation (simulated cameras)
admin_dashboard/    React + TypeScript dashboard (dist/ = prebuilt)
mobile_app/         Flutter patient app
data/               maps/ (downloaded OpenStreetMap areas), network.demo.json (offline grid), videos/samples (CC BY 4.0 clip)
docs/               architecture, flowcharts, API, database, testing, results, demo script
results/            experiment and CV evaluation outputs (JSON + CSV)
scripts/            setup/run for macOS/Linux and Windows
hardware/           Arduino (USB serial) and ESP32 (Wi-Fi) LED traffic-light firmware
```

## Limitations

Roads are real (OpenStreetMap) but traffic, ambulances and signal timing are simulated; OSM signal tagging is incomplete, so some signals are inferred and plans are generic two-phase fixed-time plans. LED / external signals are for model signals and read-only mirrors – controlling street signals needs a certified controller and the traffic authority's approval. Car-following models do not capture two-wheelers, lane indiscipline or drivers yielding to sirens. Simulated-camera accuracy is measured on rendered frames; real-video accuracy depends on camera angle and needs your own labelled clips (see docs/video.md). Thresholds are design parameters, not calibrated values. The admin API has an optional token but no user authentication. See [docs/architecture.md](docs/architecture.md#limitations-to-state-in-the-report).

Map data © OpenStreetMap contributors (ODbL).
