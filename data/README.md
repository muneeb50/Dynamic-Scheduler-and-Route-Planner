# Map data

| File | What it is |
|---|---|
| `maps/` | Real map areas downloaded from OpenStreetMap by the dashboard (**Live map → Map area**) or `simulation/tools/fetch_osm.py`: `<area>.osm.json.gz` (raw Overpass data), `<area>.network.json` (built network), `current.json` (the map loaded at start). Created on your machine, not committed. |
| `network.demo.json` | Bundled stylised 5×5 grid over the Nerul–Seawoods area, used offline until a real map has been downloaded. **Not real road geometry.** Regenerate with `python simulation/tools/make_demo_network.py`. |
| `network.json` | Optional manual import (`simulation/tools/osm_to_network.py --out data/network.json`). If it exists it **overrides** the dashboard's map choice – delete it to use Map area again. |
| `field_junctions.json` | Optional LED / external signals (Signals → advanced). |

## Getting real roads

On the first start the backend downloads Nerul – Seawoods automatically (internet needed once) and switches to it. To choose another area use **Live map → Map area** (presets, place search, "load visible area", or upload an `.osm` export), or the command line:

```bash
python simulation/tools/fetch_osm.py --list
python simulation/tools/fetch_osm.py --area navi-mumbai
python simulation/tools/build_sumo_net.py            # only if you use SUMO (after switching the backend to the new map)
```

How the importer handles junctions, roundabouts, signals (OSM-tagged and inferred), hospitals and ambulance bases: [docs/maps.md](../docs/maps.md).

**Check the import.** Open the Intersections tab: junctions marked *inferred* had no signal tagged in OSM. You can still override with a manual import:

* `--hospitals data/hospitals.json` – your own list `[{"id": "H1", "name": "...", "lat": 19.0..., "lon": 73.0...}]`.
* `--bases data/bases.json` – ambulance base locations in the same format.
* `--guess-signals 0` – only OSM-tagged signals; or edit `"signal": true/false` on nodes in the network file.

If Overpass is unavailable, export the area from <https://www.openstreetmap.org/export> and upload it in the dashboard (or run `osm_to_network.py --in export.osm`).

Map data © OpenStreetMap contributors, available under the Open Database License (ODbL). Credit OpenStreetMap in your report and slides.
