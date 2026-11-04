# Database

SQLAlchemy models live in `backend/app/models.py`. SQLite is the default (`backend/resqroute.db`, created on startup). Set `DATABASE_URL` for PostgreSQL; when PostGIS is available the backend adds **generated geometry columns** (and GiST indexes) on startup:

| Table | Generated PostGIS column |
|---|---|
| `bookings` | `pickup_geom geometry(Point,4326)` from `pickup_lon`, `pickup_lat` |
| `routes` | `geom geometry(LineString,4326)` from `geometry_geojson` |
| `intersections` | `geom geometry(Point,4326)` |
| `hospitals` | `geom geometry(Point,4326)` |

So spatial SQL works directly, e.g. bookings within 500 m of a hospital:

```sql
SELECT b.id, h.name
FROM bookings b JOIN hospitals h ON ST_DWithin(b.pickup_geom::geography, h.geom::geography, 500);
```

## Tables

| Table | Key fields | Notes |
|---|---|---|
| `simulation_runs` | engine, demand, seed, priority_enabled, recovery_enabled, queue_source, started_at, ended_at | One row per live run (reset = new run). |
| `users` | name, phone (unique) | Basic patient identity – no authentication in the prototype. |
| `hospitals` | id, name, lat, lon, node | Real-world reference data from the network file. |
| `ambulances` | id, status, base_node, current_lat/lon, position_source | `position_source = simulated`. |
| `bookings` | user_id, run_id, ambulance_id, hospital_id, pickup_lat/lon, pickup_node, status, eta_s, failure_reason, timestamps | Lifecycle state. |
| `routes` | booking_id, leg, edges (JSON), geometry_geojson, length_m, estimated_time_s | One row per leg actually driven. |
| `intersections` | id (node), lat, lon, approaches, signal_config (phase groups) | Mapped signalised junctions. |
| `signal_events` | run_id, intersection_id, booking_id, event_type, sim_time, payload | History of priority requests/grants/releases/timeouts, priority-driven transitions, safety violations. Never overwritten. |
| `traffic_snapshots` | run_id, intersection_id, sim_time, queue_lengths (JSON), source | Every 10 s; `source` = `simulated` or `cv_estimated`. |
| `recovery_events` | run_id, recovery_key, intersection_id, booking_id, status, started_sim, completed_sim, duration_s, details | Separate from signal events: one recovery spans several phases. |
| `journey_metrics` | booking_id, run_id, status, travel_time_s, stops, signal_delay_s, priority_enabled, metrics (JSON) | Written when a journey ends. |
| `system_events` | run_id, booking_id, event_type, sim_time, payload | Booking/dispatch history. |
| `experiments` | status, params, progress, results, error | Matched experiment jobs. |

Timestamps are timezone-aware UTC; simulated time (`sim_time`, seconds since run start) is stored alongside so events can be ordered within a run.
