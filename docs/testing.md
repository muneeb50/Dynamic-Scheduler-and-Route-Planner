# Testing

```bash
cd backend
pytest -q            # 61 tests, ~45 s (SUMO / YOLO / MQTT tests are skipped automatically if not installed)
```

| Blueprint scenario | Automated test |
|---|---|
| Valid ambulance booking → created and assigned | `test_api.py::test_booking_lifecycle` |
| No ambulance available → appropriate status, assigned later | `test_core.py::test_no_ambulance_available_then_assigned_when_free`, `test_api.py::test_no_ambulance_then_cancel` |
| Ambulance moving, synchronised positions | `test_api.py::test_simulation_control_and_websocket` (WebSocket payload), lifecycle test |
| Ambulance approaching a signal → correct intersection | `test_core.py::test_priority_only_at_intersections_on_route` |
| Conflicting phase active → safe transition | `test_core.py::test_signal_controller_never_runs_conflicting_greens`, `test_priority_respects_min_green_and_amber` |
| Ambulance clears the junction → priority released | `test_core.py::test_release_happens_after_passing_the_junction` |
| Congestion accumulates → recovery detects backlog; traffic returns → logged | `test_core.py::test_recovery_terminates_and_is_bounded` |
| Priority actually helps (averaged over seeds) | `test_core.py::test_priority_reduces_signal_delay_on_average` |
| Baseline has no priority/recovery | `test_core.py::test_baseline_has_no_priority_or_recovery` |
| CV miscounts → handled safely | `test_osm_cv_experiments.py::test_cv_detects_and_flags_stopped_vehicles` + conservative `queue_used` logic |
| WebSocket disconnects → stale indicator + reconnect | Client-side (dashboard `useSimulation`, Flutter `TrackingService`); verify manually by stopping the backend for 10 s |
| Ambulance cannot progress → timeout, not false completion | `test_core.py::test_blocked_ambulance_times_out_instead_of_completing` |
| Cancellation | `test_core.py::test_cancel_releases_ambulance` |
| Invalid input / outside service area | `test_api.py::test_validation_errors`, `test_core.py::test_pickup_outside_service_area_rejected` |
| OSM import (oneways, signals on approach nodes, SCC) | `test_osm_cv_experiments.py::test_osm_import` |
| Matched experiment + files | `test_osm_cv_experiments.py::test_small_experiment_is_matched_and_safe` |
| SUMO engine end to end | `test_osm_cv_experiments.py::test_sumo_engine_trial` |
| Tracker keeps ids, counts line crossings, detects a stopped vehicle | `test_video.py::test_tracker_and_line_crossing` |
| OpenCV motion pipeline on a video → annotated video + summary | `test_video.py::test_motion_pipeline_on_video` |
| YOLO (PyTorch) and YOLO via OpenCV DNN detect a car in the sample video | `test_video.py::test_yolo_detects_car_in_sample`, `test_opencv_dnn_yolo_detects_car` |
| Video upload, batch outputs, browser-camera push, camera → controller feed, input validation | `test_video.py::test_video_api_and_controller_feed` |
| Ambulance vs other vehicles (flashing beacon vs red car) | `test_ambulance.py::test_beacon_cue_distinguishes_ambulance_from_red_car` |
| Ambulance enters zone, then passes the stop line | `test_ambulance.py::test_corridor_events_in_zone_then_passed` |
| Camera sees ambulance → real-time signal green → released after passing | `test_ambulance.py::test_camera_ambulance_turns_field_signal_green`, `test_field.py::test_camera_triggers` |
| GPS corridor: green before arrival, release after passing | `test_field.py::test_gps_corridor_green_then_release` |
| Priority safe (amber/all-red, no crossing greens), held, compensation, back to normal | `test_field.py::test_priority_is_safe_and_returns_to_normal`, `test_second_request_is_queued` |
| Open formats: J2735 SPaT, SensorThings, simple JSON, MQTT | `test_field.py::test_spat_round_trip`, `test_state_normalisation`, `test_http_driver_and_sensorthings_source`, `test_mirror_mode_is_advisory`, `test_mqtt_spat_round_trip` |
| Physical outputs: serial (loopback) and HTTP | `test_field.py::test_serial_driver_loopback`, `test_http_driver_and_sensorthings_source` |
| Real-map import: dual carriageways and roundabouts consolidated, OSM signals mapped, main-road signals inferred, hospitals/bases, connectivity | `test_maps_scale.py::test_osm_import_consolidates_junctions_and_maps_signals` |
| Demand scales with the map; city-scale traffic (>1000 vehicles) fast and with 0 conflicting greens | `test_maps_scale.py::test_demand_scales_with_the_map_and_runs_city_scale` |
| Camera / operator priority on a simulated signal: safe green, hold, release → recovery, waiting request, max-hold timeout | `test_maps_scale.py::test_external_priority_green_release_and_recovery` |
| Test ambulance through any junction gets green and is released after passing | `test_maps_scale.py::test_test_ambulance_drives_through_with_priority` |
| Upload a map → live switch → booking, operator trigger, test ambulance and camera-driven simulated signal on the new map | `test_maps_scale.py::test_map_upload_switches_network_and_camera_drives_sim_signal` |
| Webcam "all objects" mode draws every object but counts only road vehicles | `test_maps_scale.py::test_all_objects_mode_draws_everything_but_counts_road_users` |
| Detector self-test endpoint | `test_maps_scale.py::test_detector_selftest_endpoint` |
| Compact live snapshot (vehicles + signals) | `test_maps_scale.py::test_compact_live_snapshot` |
| Photo analysis: counts, occupancy, ambulance light bar vs plain van; photo API, bad files, path safety | `test_photos_perf.py::test_photo_analysis_counts_and_ambulance_light_bar`, `test_photo_api` |
| Slow ambulance model runs in the background without holding up frames | `test_photos_perf.py::test_background_ambulance_model_does_not_block` |
| Browser camera survives bad frames, keep-alive, reconnects and a backend restart (source re-created) | `test_photos_perf.py::test_browser_camera_survives_reconnect_and_restart` |
| Events persisted by the background DB writer, visible to reads | `test_photos_perf.py::test_events_are_persisted_off_the_event_loop` |

Mobile app: `cd mobile_app && flutter test` (model parsing). Dashboard: `npm run build` type-checks everything.

## Manual checks before the demo

1. Stop the backend while the dashboard is open → badge turns orange ("Reconnecting"); restart → back to "Live".
2. Book from the phone and the web page at the same time → both ambulances tracked; a third/fourth booking waits.
3. Toggle "Queues from computer vision" on the Live page and run a booking → priority still works; the Computer vision page shows the frames being used.
4. Set demand to high, disable priority, book; enable priority, book the same pickup → compare the two bookings on the Bookings page (single runs are anecdotal – use the Analytics experiment for claims).
