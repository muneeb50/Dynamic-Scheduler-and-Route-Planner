# Real maps (OpenStreetMap) and city-scale simulation

The simulation runs on **real roads from OpenStreetMap**, downloaded through the open
[Overpass API](https://wiki.openstreetmap.org/wiki/Overpass_API). Place search uses
[Nominatim](https://nominatim.org/). Both are free, open-source services. The dashboard
background is the standard OpenStreetMap tile layer.

## How it works

1. **First start.** The backend starts with the bundled offline grid, then downloads the
   **Nerul – Seawoods** area in the background (about 13 km², every street). When the build
   finishes, it switches to the real map automatically and the dashboard reloads the roads.
   This usually takes 10–60 s, depending on Overpass. The downloaded map is saved and becomes
   the default from then on, so it also works offline.
2. **Changing the area.** Go to **Live map → Map area**. You can:
   - pick a **preset**: Nerul – Seawoods, Vashi, CBD Belapur – Kharghar, Navi Mumbai south, or Navi Mumbai (Vashi → Belapur, city scale);
   - **search** any place (Nominatim) and load its area;
   - pan or zoom the map and click **Load visible area**;
   - **upload** an `.osm` file exported from <https://www.openstreetmap.org/export>. This works when Overpass is busy or blocked;
   - go back to the **offline demo grid**.
3. Downloads are cached in `data/maps/` (`<area>.osm.json.gz` is the raw data, `<area>.network.json` is the built network, and `current.json` marks the map to load at the next start).

Command line equivalents:
```bash
python simulation/tools/fetch_osm.py --list
python simulation/tools/fetch_osm.py --area navi-mumbai
python simulation/tools/fetch_osm.py --bbox 19.010 73.005 19.050 73.045 --name "My area"
python simulation/tools/osm_to_network.py --in export.osm --out data/network.json   # data/network.json overrides everything
```
Environment variables: `MAP_AREA=vashi` sets the area downloaded on first start; `MAP_AUTO_FETCH=false` stays on the demo grid.

## What the importer does (`backend/app/mapdata/osm.py`)

| Step | Detail |
|---|---|
| Roads | Drivable `highway` ways, respecting `oneway`, `junction=roundabout`, `lanes`, `lanes:forward/backward`, `maxspeed` and `access`. Areas up to 40 km² keep **every street**; larger areas keep **main roads** (motorway … tertiary and their links). Indian urban default speeds are used when `maxspeed` is missing. |
| Geometry | Ways are split at junctions. Each road segment keeps the real shape points of the way. |
| Junctions | **Consolidated**: OSM draws a dual-carriageway crossing as 2–4 nodes and a roundabout as a ring of nodes. Nodes within 12 m, short links of 28 m or less between two real intersections, and every node of a roundabout up to 90 m across are merged into one junction (max 70 m extent). This is what traffic simulators such as SUMO (`junctions.join`) and OSMnx (`consolidate_intersections`) do too. |
| Connectivity | Only the largest strongly connected component is kept, so every trip has a route. |
| Signals | `highway=traffic_signals` nodes are usually tagged on the stop lines of the approaches, so they are mapped to the nearest junction within 50 m. Pedestrian-only signals and roundabouts are skipped. Tagging in Indian cities is incomplete, so a signal is also **inferred** where two different main roads cross (one secondary or better, the other tertiary or better), at least 180 m from another signal. The dashboard marks inferred signals. |
| Hospitals | `amenity=hospital` or `healthcare=hospital` (nodes and building outlines, de-duplicated) become destinations. |
| Ambulance bases | Placed at fire stations and hospitals, spread over the area (farthest-point sampling), with 3–12 bases depending on the size of the area. |
| Names | Junctions are named after the crossing roads, for example "Palm Beach Road × Sector 19 Road". |

**Signals are simulated.** Each signal runs a simulated fixed-time plan (25 s green, 3 s amber, 2 s all-red per phase, with an offset per junction). It uses emergency priority and adaptive recovery. No live signal data is used.

## Large amounts of traffic

- **Demand scales with the map.** The demand levels (`low`, `medium`, `high`, `peak`) are defined for the demo network. They are multiplied by the network size (≈ lane-km^0.85), so traffic density is similar on any map. The Navi Mumbai preset gets about 18× the demo demand.
- **City-wide trips.** On real maps, 70% of trips start and end at junctions inside the city, weighted by road importance. The other 30% are through traffic between the edges of the map. Routes come from cached shortest-path trees, one Dijkstra per origin.
- **Random emergencies.** In **Live map → Simulation control**, *Random emergencies* creates N real bookings per simulated hour, at random places, sent to the nearest hospital. Each is dispatched, prioritised at every signal on its route, and recovered afterwards.
- **Fast enough.** The engine only works on lanes that hold vehicles, caches turn angles and bearings, and finds positions on curved roads with a binary search. The live WebSocket sends vehicles and signals in a compact form, and the map draws vehicles and signals on canvas layers.

### Benchmark

`python simulation/benchmark.py` measures the current map. Use `--synthetic 6 5` for an offline synthetic test city. Results on the development machine (one CPU core; your laptop may be faster or slower):

**Synthetic 6 × 5 km city, all streets:** 779 junctions, 2,579 segments, 636 lane-km, 85 signals, 15 simulated minutes, with 3 test ambulances.

| demand | vehicles / h | peak on the road | trips | real-time factor | priority granted | conflicting greens |
|---|---|---|---|---|---|---|
| low | 6,099 | 818 | 753 | 165× | 5/5 | 0 |
| medium | 12,197 | 1,582 | 1,477 | 103× | 5/5 | 0 |
| high | 18,296 | 2,469 | 2,058 | 72× | 5/5 | 0 |
| peak | 24,395 | 3,531 | 2,467 | 55× | 5/5 | 0 |

**Synthetic 14 × 12 km city, main roads:** 725 junctions, 2,792 segments, 623 signals, medium demand of 32,600 vehicles/h. It reaches about 8,000 vehicles on the road after 20 simulated minutes and still runs about 11× faster than real time.

The synthetic city (`simulation/tools/synthetic_city.py`) is **not real data**. It copies the awkward parts of OSM: curved roads, dual carriageways, roundabouts, signals tagged on the approaches, and hospitals drawn as outlines. That lets the importer and the simulator be tested at scale without internet. Re-run the benchmark on the real map you load and use those numbers in your report.

## Limitations
- OSM completeness varies: missing lane counts, missing `maxspeed` and untagged signals are filled with defaults or inference. Check the imported junctions for your study area.
- Signal plans are generic fixed-time plans, not the real timings of Navi Mumbai's signals, and turning movements are not separately signalised (two-phase control).
- Traffic is car-following only (no two-wheeler filtering or lane indiscipline).
- Overpass and Nominatim are shared public services: keep areas reasonable (the limit is 900 km²) and expect occasional "busy" errors. Retry, or upload an `.osm` export instead.

Map data © OpenStreetMap contributors, ODbL.
