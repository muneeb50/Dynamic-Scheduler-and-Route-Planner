# Architecture

ResQRoute AI is one Python backend with clearly separated modules, two clients and an optional SUMO engine. Diagrams are in [flowcharts.md](flowcharts.md).

## Modules (backend/app)

| Module | Responsibility |
|---|---|
| `mapdata/*` | Real maps: Overpass / Nominatim clients, OSM → network importer (junction consolidation, roundabouts, signal mapping and inference, hospitals, bases), area presets, cache and background loader; the live service hot-swaps the network ([maps.md](maps.md)). |
| `core/network.py` | Road graph loaded from the current map (OpenStreetMap area or the demo grid): nodes, directed edges with real geometry, lanes, speed limits, hospitals, ambulance bases. Dijkstra routing, signal phase grouping, lat/lon ↔ metres. |
| `sim/base.py` | `TrafficEngine` interface + Poisson background demand (seeded, so matched runs get identical traffic). |
| `sim/builtin.py` | Pure-Python microscopic simulator: IDM car-following per lane, no overtaking, amber dilemma-zone logic, spill-back blocking, gridlock breaker. |
| `sim/sumo_engine.py` | Same interface over SUMO/TraCI (subscriptions, full control of every traffic light). |
| `sim/coordinator.py` | Advances time; dispatch, emergency-aware routing, journey lifecycle (pickup, dwell, hospital, return), timeouts, metrics, snapshots. No web/DB code – used by the API, tests and experiments alike. |
| `traffic_control/signal.py` | Per-junction controller. Only GREEN→AMBER→ALL-RED→GREEN transitions exist; conflicting greens are impossible by construction and re-checked every tick. |
| `traffic_control/priority.py` | Route-aware priority: looks ahead along the ambulance's planned route, computes ETA and queue-clearance lead time, requests the phase, releases only after the ambulance is observed past the junction. *External* requests (a camera that recognised an ambulance on an approach, an operator) use the same controller path and end in the same recovery. |
| `recovery/engine.py` | Pre-priority baselines, backlog ΔQ, bounded adaptive green times, completion / timeout. |
| `analytics/experiment.py` | Matched baseline-vs-priority experiments with means, SD, 95% CI and paired differences. |
| `computer_vision/video/*` | Real-video analytics: YOLO (Ultralytics), YOLO via OpenCV DNN (ONNX) and MOG2 motion detectors; tracker; zone/line/queue/flow/congestion analytics; threaded sources (file live/batch, RTSP/HTTP, webcam, browser push); see [video.md](video.md). |
| `field/*` | Real-time ("field") junctions: wall-clock signal controllers, open-format live inputs (SPaT, SensorThings, MQTT, JSON), outputs (virtual, Arduino serial, HTTP, MQTT, SPaT), camera and GPS corridor priority with compensation recovery; see [signals.md](signals.md). |
| `services/live.py` | Runs the coordinator in real time, persists events, WebSocket pub/sub. |
| `api/*` | REST + WebSocket endpoints (see [api.md](api.md)). |

## Key design decisions

**One road network, many consumers.** The network file (an OpenStreetMap area built by `mapdata`, or the demo grid) is the single source of truth. The SUMO network is generated from it with identical ids and the same metric projection, so no map-matching between a routing engine and the simulator is needed. This replaces OSRM from the original blueprint: an external router would use a different graph than the simulator and could not see simulated queues.

**Emergency-aware routing.** Edge cost = free-flow time at ambulance speed × spill-back penalty + queue discharge time at signalised junctions (+ expected red wait when priority is off). It is recomputed at the start of every leg.

**Engine-agnostic control.** Signal controllers, priority and recovery only use `halting(edge)`, `vehicle_state(id)`, `vehicles_ahead(id)` and `set_signal_state(...)`. The same logic drives the built-in simulator and SUMO.

**Left-hand traffic.** Network rendering, SUMO (`--lefthand`) and turn handling assume Indian (left-hand) traffic; the right-turn across oncoming traffic is a permitted (yielding) movement in SUMO.

**Live cameras can drive the controller.** A video source attached to a simulated junction approach turns that approach green when it recognises an ambulance in its zone and releases it when the ambulance crosses the stop line (then recovery). It also writes its measured queue into `Coordinator.external_queues`; `queue_for()` prefers a fresh camera value (≤ 5 s old) over CV-on-simulation or simulator ground truth, so priority lead times and recovery react to real traffic in front of the camera.

**Computer vision is a measured subsystem, not a dependency.** The controller runs on simulator queues by default. With `QUEUE_SOURCE=cv`, cameras at the intersections the controller is reasoning about are processed at 1 Hz; low-confidence estimates are combined conservatively so a bad frame cannot make the controller under-estimate a queue.

**Everything is logged.** Signal events, traffic snapshots, recovery events and journey metrics are stored as history (never overwritten), with simulation run id, simulated time and UTC wall-clock time.

## Data flow for one booking

1. `POST /api/bookings` → booking row → `Coordinator.create_journey` snaps the pickup to the nearest junction and assigns the ambulance with the lowest emergency-aware ETA.
2. Each step (0.5 s simulated): signals tick → engine moves vehicles → queues sampled (1 Hz) → recovery updated → priority manager evaluates each ambulance → journey progress checked.
3. Every event (assignment, priority request/grant/release, recovery start/finish, arrival, failure) is persisted and streamed to `/ws/simulation` and `/ws/bookings/{id}`.
4. On arrival, journey metrics are computed and stored.

## Limitations to state in the report

* Traffic, ambulances and signal timing are simulated on real OpenStreetMap roads. No real signals are controlled and no live signal data is used.
* OSM signal tagging and lane counts may be incomplete – signals are inferred at main-road crossings and defaults fill missing tags; check the imported junctions. Without internet on first start the stylised demo grid is used.
* Real Indian traffic has two-wheelers, lane indiscipline and vehicles yielding to ambulances; the car-following models here do not represent these.
* CV accuracy is measured on rendered frames; real CCTV needs YOLO (`computer_vision/detect_video.py`) and its own evaluation.
* Recovery thresholds (tolerance, gain, max duration) are configurable defaults, not calibrated values.
