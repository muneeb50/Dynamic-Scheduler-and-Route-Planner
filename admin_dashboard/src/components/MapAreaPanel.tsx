import { useEffect, useState } from "react";
import { API_BASE, MapAreas, NetworkInfo, PlaceHit, SimSnapshot, api, localStorageGet, post } from "../api";

const MAX_KM2 = 900;
const km2 = (b: number[]) => {
  const [s, w, n, e] = b;
  const h = (n - s) * 111.2;
  const wd = (e - w) * 111.2 * Math.cos((((s + n) / 2) * Math.PI) / 180);
  return Math.abs(h * wd);
};

/** Choose the real-world area the simulation runs on (OpenStreetMap via the Overpass API). */
export default function MapAreaPanel({ network, snap, viewBox, onPreview }: {
  network: NetworkInfo; snap: SimSnapshot | null; viewBox: number[] | null; onPreview: (b: number[] | null) => void;
}) {
  const [areas, setAreas] = useState<MapAreas | null>(null);
  const [open, setOpen] = useState(network.source !== "osm");
  const [preset, setPreset] = useState("");
  const [detail, setDetail] = useState("auto");
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<PlaceHit[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const state = snap?.map?.state ?? areas?.status.state;
  const loading = state === "queued" || state === "downloading" || state === "building";

  const reload = () => api<MapAreas>("/api/map/areas").then(setAreas).catch(() => undefined);
  useEffect(() => { reload(); }, [network.network_version]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!preset && areas?.areas.length) setPreset(areas.current.key ?? areas.areas[0].id);
  }, [areas]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const a = areas?.areas.find((x) => x.id === preset);
    onPreview(open && a?.bbox ? a.bbox : null);
  }, [preset, open]); // eslint-disable-line react-hooks/exhaustive-deps

  const run = async (body: Record<string, unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await post("/api/map/load", { detail, ...body });
      reload();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const search = async () => {
    setError(null);
    setHits(null);
    try {
      setHits(await api<PlaceHit[]>(`/api/map/search?q=${encodeURIComponent(q)}`));
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const upload = async (f: File) => {
    setBusy(true);
    setError(null);
    try {
      const fd = new FormData();
      fd.append("file", f);
      fd.append("name", f.name.replace(/\.(osm|json|xml)(\.gz)?$/i, ""));
      fd.append("detail", detail);
      const token = localStorageGet("adminToken");
      const r = await fetch(`${API_BASE}/api/map/upload`, { method: "POST", body: fd, headers: token ? { "X-Admin-Token": token } : {} });
      if (!r.ok) throw new Error((await r.json()).detail ?? r.statusText);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const meta = network.meta ?? {};
  const view = viewBox ? km2(viewBox) : 0;

  return (
    <div className="card">
      <div className="row between">
        <div className="card-title" style={{ marginBottom: 0 }}>Map area</div>
        <button className="btn ghost small-btn" onClick={() => setOpen(!open)}>{open ? "Hide" : "Change"}</button>
      </div>
      <div className="small" style={{ marginTop: 6 }}>
        <b>{network.name}</b>{" "}
        <span className="muted">
          {network.source === "osm" ? "· real roads from OpenStreetMap" : "· offline demo grid (not real roads)"}
        </span>
        <div className="muted">
          {network.nodes.toLocaleString()} junctions · {network.edges.toLocaleString()} road segments · {network.lane_km ?? "–"} lane-km ·{" "}
          {network.signals} signals{meta.osm_signals !== undefined ? ` (${meta.osm_signals} mapped in OSM, ${meta.inferred_signals} inferred at main-road crossings)` : ""}
          {meta.area_km2 ? ` · ${meta.area_km2} km² · ${meta.detail === "main" ? "main roads" : "all streets"}` : ""}
        </div>
      </div>
      {loading && <div className="map-progress">⏳ {snap?.map?.message}</div>}
      {state === "error" && <div className="error">{snap?.map?.message ?? areas?.status.message}</div>}
      {open && (
        <div className="col" style={{ gap: 8, marginTop: 8 }}>
          <div className="row gap wrap">
            <select value={preset} onChange={(e) => setPreset(e.target.value)} style={{ flex: 1, minWidth: 180 }}>
              {areas?.areas.map((a) => (
                <option key={a.id} value={a.id}>{a.name}{a.area_km2 ? ` · ${a.area_km2} km²` : ""}{a.cached ? " ✓" : ""}</option>
              ))}
            </select>
            <button className="btn" disabled={busy || loading || !preset} onClick={() => run({ area: preset })}>Load</button>
          </div>
          <div className="row gap wrap">
            <input placeholder="Search a place (e.g. Kharghar, Thane, Pune)…" value={q} onChange={(e) => setQ(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && q.trim().length > 1 && search()} style={{ flex: 1, minWidth: 180 }} />
            <button className="btn ghost" disabled={q.trim().length < 2} onClick={search}>Search</button>
          </div>
          {hits && hits.length === 0 && <div className="muted small">No places found.</div>}
          {hits?.map((h) => {
            const a = km2(h.bbox);
            return (
              <div key={`${h.lat},${h.lon}`} className="kv clickable" onMouseEnter={() => onPreview(h.bbox)}
                onClick={() => a <= MAX_KM2 ? run({ bbox: h.bbox, name: h.name.split(",")[0] }) : setError(`${h.name.split(",")[0]} is ${Math.round(a)} km² – too large. Zoom the map to the part you want and use “Load visible area”.`)}>
                <b>{h.name.split(",").slice(0, 2).join(",")}</b> <span className="muted small">{h.type} · {a < 1 ? a.toFixed(1) : Math.round(a)} km²</span>
              </div>
            );
          })}
          <div className="row gap wrap">
            <button className="btn ghost" disabled={busy || loading || !viewBox || view > MAX_KM2 || view < 0.3}
              onClick={() => viewBox && run({ bbox: viewBox, name: "Visible area" })}>
              Load visible area ({view < 1 ? view.toFixed(1) : Math.round(view)} km²)
            </button>
            <label className="field-inline small">Detail
              <select value={detail} onChange={(e) => setDetail(e.target.value)}>
                <option value="auto">auto (all streets up to 40 km²)</option>
                <option value="full">all streets</option>
                <option value="main">main roads only</option>
              </select>
            </label>
          </div>
          <div className="row gap wrap small">
            <label className="btn ghost file-btn">Upload .osm file
              <input type="file" accept=".osm,.xml,.json,.gz" onChange={(e) => e.target.files?.[0] && upload(e.target.files[0])} />
            </label>
            <button className="btn ghost" disabled={busy || loading} onClick={() => run({ area: "demo" })}>Offline demo grid</button>
            {preset && areas?.areas.find((a) => a.id === preset)?.cached && (
              <button className="btn ghost" disabled={busy || loading} onClick={() => run({ area: preset, refresh: true })}>Re-download</button>
            )}
          </div>
          <div className="muted small">
            Roads, lanes, speed limits, one-ways, roundabouts, traffic signals and hospitals come from OpenStreetMap
            (Overpass API); place search uses Nominatim. Downloads are cached in <code>data/maps</code>, so a loaded area
            also works offline. Signal timing is simulated. Larger areas use main roads only. Map data © OpenStreetMap contributors.
          </div>
          {error && <div className="error">{error}</div>}
        </div>
      )}
    </div>
  );
}
