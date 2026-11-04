import { Fragment, useEffect, useRef, useState } from "react";
import L from "leaflet";
import { CircleMarker, MapContainer, Marker, Polyline, TileLayer, Tooltip, useMapEvents } from "react-leaflet";
import "leaflet/dist/leaflet.css";
import {
  API_BASE, FieldAmbulance, FieldJunction, FieldSnapshot, Lamp, NetworkInfo, VideoSourceInfo, api, post, wsUrl,
} from "../api";
import { Stat } from "../components/Panels";
import { usePoll } from "../hooks/useSimulation";

const LAMP: Record<Lamp, string> = { G: "#30d158", Y: "#ffd60a", R: "#ff453a", X: "#3a3a3c" };
const MODE_COLOR: Record<string, string> = { NORMAL: "#8b8d98", PRIORITY: "#8e4ec6", RECOVERY: "#f76b15", MIRROR: "#0090ff" };
const EVENT_TEXT: Record<string, (e: Record<string, unknown>) => string> = {
  PRIORITY_REQUESTED: (e) => `Green requested for ${e.approach} (${e.trigger}${e.distance_m ? `, ${e.distance_m} m away, ETA ${e.eta_s}s` : ""}${e.camera ? `, camera ${e.camera}` : ""})`,
  PRIORITY_GRANTED: (e) => `Green given to ${e.approach} after ${e.after_s}s (amber + all-red first)`,
  PRIORITY_RELEASED: (e) => `Ambulance ${String(e.reason).toLowerCase().replace(/_/g, " ")} – priority released after ${e.hold_s}s hold`,
  PRIORITY_QUEUED: (e) => `Request for ${e.approach} queued (junction busy with ${e.busy_with})`,
  PRIORITY_ADVISORY: (e) => `ADVISORY: give green to ${e.approach} (${e.trigger}) – source is read-only (now ${e.current})`,
  RECOVERY_TIMING: (e) => `Recovery: group ${Number(e.group) + 1} green ${e.green_s}s (compensation ${e.compensation_s}s, camera backlog ${e.camera_backlog_per_lane}/lane)`,
  SAFETY_VIOLATION: () => "SAFETY VIOLATION",
  GPS_SIMULATION_STARTED: (e) => `Simulated drive started for ${e.ambulance}`,
  GPS_SIMULATION_FINISHED: (e) => `Simulated drive finished for ${e.ambulance}`,
};

function useField() {
  const [snap, setSnap] = useState<FieldSnapshot | null>(null);
  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let retry: number | undefined;
    const connect = () => {
      ws = new WebSocket(wsUrl("/ws/field"));
      ws.onmessage = (m) => setSnap(JSON.parse(m.data));
      ws.onclose = () => { if (!closed) retry = window.setTimeout(connect, 2000); };
    };
    connect();
    const ping = window.setInterval(() => ws?.readyState === WebSocket.OPEN && ws.send("ping"), 10000);
    return () => { closed = true; window.clearTimeout(retry); window.clearInterval(ping); ws?.close(); };
  }, []);
  return snap;
}

// --------------------------------------------------------------- junction graphic
function JunctionGraphic({ j }: { j: FieldJunction }) {
  const size = 300, c = size / 2;
  return (
    <svg viewBox={`0 0 ${size} ${size}`} className="junction-svg">
      <rect x="0" y="0" width={size} height={size} rx="12" fill="#101218" />
      {j.approaches.map((a) => {
        const rad = (a.bearing * Math.PI) / 180;
        const dx = Math.sin(rad), dy = -Math.cos(rad);
        return <line key={`road-${a.id}`} x1={c} y1={c} x2={c + dx * 150} y2={c + dy * 150} stroke="#2b2f3a" strokeWidth="46" />;
      })}
      <circle cx={c} cy={c} r="30" fill="#2b2f3a" />
      {j.approaches.map((a) => {
        const rad = (a.bearing * Math.PI) / 180;
        const dx = Math.sin(rad), dy = -Math.cos(rad);
        const x = c + dx * 92, y = c + dy * 92;
        const st = j.states[a.id] ?? "X";
        const active = j.active?.approach === a.id;
        return (
          <g key={a.id} transform={`translate(${x},${y})`}>
            <rect x="-14" y="-38" width="28" height="76" rx="7" fill="#05060a" stroke={active ? "#8e4ec6" : "#3a3f4b"} strokeWidth={active ? 4 : 2} />
            {(["R", "Y", "G"] as Lamp[]).map((l, i) => (
              <circle key={l} cx="0" cy={-23 + i * 23} r="9" fill={st === l ? LAMP[l] : "#1c1d22"}
                style={st === l ? { filter: `drop-shadow(0 0 6px ${LAMP[l]})` } : undefined} />
            ))}
            <text x={dx >= 0 ? 22 : -22} y="5" fill="#e7e9ee" fontSize="15" fontWeight="700" textAnchor={dx >= 0 ? "start" : "end"}>{a.id}</text>
            {j.remaining[a.id] !== undefined && j.remaining[a.id] < 900 && (
              <text x={dx >= 0 ? 22 : -22} y="22" fill="#8b8fa0" fontSize="11" textAnchor={dx >= 0 ? "start" : "end"}>{Math.round(j.remaining[a.id])}s</text>
            )}
          </g>
        );
      })}
      <text x={c} y={c + 5} fill={MODE_COLOR[j.mode_now] ?? "#8b8d98"} fontSize="12" fontWeight="700" textAnchor="middle">{j.mode_now}</text>
    </svg>
  );
}

// ---------------------------------------------------------------- corridor map
const ambIcon = L.divIcon({ html: '<div class="pin pin-amb busy">✚</div>', className: "map-icon", iconSize: [28, 28], iconAnchor: [14, 14] });

function Picker({ onPick }: { onPick?: (p: [number, number]) => void }) {
  useMapEvents({ click: (e) => onPick?.([e.latlng.lat, e.latlng.lng]) });
  return null;
}

function CorridorMap({ snap, selected, onSelect, onPick, pathPreview, trails }: {
  snap: FieldSnapshot; selected: string | null; onSelect: (id: string) => void; onPick?: (p: [number, number]) => void;
  pathPreview: [number, number][]; trails: Record<string, [number, number][]>;
}) {
  const first = snap.junctions[0];
  const center: [number, number] = first ? [first.lat, first.lon] : [19.03, 73.02];
  return (
    <MapContainer center={center} zoom={16} className="map" style={{ height: 380 }}>
      <TileLayer attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png" opacity={0.6} />
      <Picker onPick={onPick} />
      {snap.junctions.map((j) => (
        <Fragment key={j.id}>
          <CircleMarker center={[j.lat, j.lon]} radius={selected === j.id ? 13 : 10}
            pathOptions={{ color: MODE_COLOR[j.mode_now] ?? "#888", weight: 4, fillColor: "#11131a", fillOpacity: 0.9 }}
            eventHandlers={{ click: () => onSelect(j.id) }}>
            <Tooltip>{j.id} · {j.name} · {j.mode_now}<br />{Object.entries(j.states).map(([a, s]) => `${a}:${s}`).join(" ")}</Tooltip>
          </CircleMarker>
        </Fragment>
      ))}
      {pathPreview.length > 1 && <Polyline positions={pathPreview} pathOptions={{ color: "#e5484d", dashArray: "6 6", weight: 3 }} />}
      {Object.entries(trails).map(([id, pts]) => pts.length > 1 && (
        <Polyline key={id} positions={pts} pathOptions={{ color: "#e5484d", weight: 4, opacity: 0.6 }} />
      ))}
      {snap.ambulances.map((a: FieldAmbulance) => (
        <Marker key={a.id} position={[a.lat, a.lon]} icon={ambIcon} zIndexOffset={1000}>
          <Tooltip>{a.id}{a.speed ? ` · ${(a.speed * 3.6).toFixed(0)} km/h` : ""}{Object.keys(a.corridor).length ? ` · priority at ${Object.keys(a.corridor).join(", ")}` : ""}</Tooltip>
        </Marker>
      ))}
    </MapContainer>
  );
}

// --------------------------------------------------------------- junction form
const PRESETS: Record<string, { id: string; name: string; bearing: number }[]> = {
  "4-way": [{ id: "N", name: "from north", bearing: 0 }, { id: "E", name: "from east", bearing: 90 }, { id: "S", name: "from south", bearing: 180 }, { id: "W", name: "from west", bearing: 270 }],
  "T (N-E-S)": [{ id: "N", name: "from north", bearing: 0 }, { id: "E", name: "from east", bearing: 90 }, { id: "S", name: "from south", bearing: 180 }],
  "2-phase (N/E)": [{ id: "N", name: "main road", bearing: 0 }, { id: "E", name: "side road", bearing: 90 }],
};

function JunctionForm({ initial, picked, onSaved, onCancel }: {
  initial: FieldJunction | null; picked: [number, number] | null; onSaved: (id: string) => void; onCancel: () => void;
}) {
  const [name, setName] = useState(initial?.name ?? "New junction");
  const [lat, setLat] = useState(initial?.lat ?? 19.03);
  const [lon, setLon] = useState(initial?.lon ?? 73.024);
  const [approaches, setApproaches] = useState(initial?.approaches ?? PRESETS["4-way"].map((a) => ({ ...a, lanes: 1 })));
  const [mode, setMode] = useState<"control" | "mirror">(initial?.mode ?? "control");
  const [plan, setPlan] = useState({ green: 25, yellow: 3, all_red: 2, min_green: 7, max_green: 60, ...(initial?.plan ?? {}) });
  const [driver, setDriver] = useState<Record<string, unknown>>(initial?.driver ?? { type: "virtual" });
  const [source, setSource] = useState<Record<string, unknown>>(initial?.source ?? { type: "none" });
  const [radius, setRadius] = useState(Number(initial?.corridor?.radius_m ?? 400));
  const [error, setError] = useState<string | null>(null);
  const [test, setTest] = useState<string | null>(null);
  const { data: ports } = usePoll(() => api<{ installed: boolean; ports: { device: string; description: string }[] }>("/api/field/serial-ports"), 0);
  useEffect(() => { if (picked) { setLat(picked[0]); setLon(picked[1]); } }, [picked]);

  const setD = (k: string, v: unknown) => setDriver({ ...driver, [k]: v });
  const setS = (k: string, v: unknown) => setSource({ ...source, [k]: v });
  const jsonField = (k: string, placeholder: string) => (
    <label className="field">{k} (JSON)
      <input placeholder={placeholder} defaultValue={source[k] ? JSON.stringify(source[k]) : ""}
        onBlur={(e) => { try { setS(k, e.target.value ? JSON.parse(e.target.value) : undefined); setError(null); } catch { setError(`${k} is not valid JSON`); } }} />
    </label>
  );
  const save = async () => {
    setError(null);
    try {
      const body = { id: initial?.id, name, lat, lon, approaches, mode, plan, driver, source, corridor: { ...(initial?.corridor ?? {}), radius_m: radius } };
      const res = await post<FieldJunction>("/api/field/junctions", body);
      onSaved(res.id);
    } catch (e) { setError((e as Error).message); }
  };
  const testSource = async () => {
    setTest("testing…");
    try { setTest(JSON.stringify((await post<{ states: unknown }>("/api/field/sources/test", source)).states)); }
    catch (e) { setTest((e as Error).message); }
  };

  return (
    <div className="card">
      <div className="card-title">{initial ? `Edit ${initial.id}` : "New real-time junction"}</div>
      <div className="row gap wrap">
        <label className="field-inline">Name <input value={name} onChange={(e) => setName(e.target.value)} /></label>
        <label className="field-inline">Lat <input value={lat} onChange={(e) => setLat(Number(e.target.value))} style={{ width: 110 }} /></label>
        <label className="field-inline">Lon <input value={lon} onChange={(e) => setLon(Number(e.target.value))} style={{ width: 110 }} /></label>
        <span className="muted small">or click the map to place it</span>
      </div>
      <div className="row gap wrap" style={{ marginTop: 8 }}>
        <span className="muted small">Approaches:</span>
        {Object.keys(PRESETS).map((p) => <button key={p} className="btn ghost" onClick={() => setApproaches(PRESETS[p].map((a) => ({ ...a, lanes: 1 })))}>{p}</button>)}
      </div>
      <table className="tbl" style={{ marginTop: 6 }}>
        <thead><tr><th>Id</th><th>Name</th><th>Bearing (° from north, where traffic comes from)</th><th>Lanes</th><th /></tr></thead>
        <tbody>
          {approaches.map((a, i) => (
            <tr key={i}>
              <td><input value={a.id} style={{ width: 50 }} onChange={(e) => setApproaches(approaches.map((x, k) => k === i ? { ...x, id: e.target.value } : x))} /></td>
              <td><input value={a.name} onChange={(e) => setApproaches(approaches.map((x, k) => k === i ? { ...x, name: e.target.value } : x))} /></td>
              <td><input type="number" value={a.bearing} style={{ width: 80 }} onChange={(e) => setApproaches(approaches.map((x, k) => k === i ? { ...x, bearing: Number(e.target.value) } : x))} /></td>
              <td><input type="number" min={1} max={6} value={a.lanes} style={{ width: 60 }} onChange={(e) => setApproaches(approaches.map((x, k) => k === i ? { ...x, lanes: Number(e.target.value) } : x))} /></td>
              <td><button className="btn ghost" onClick={() => setApproaches(approaches.filter((_, k) => k !== i))}>✕</button></td>
            </tr>
          ))}
        </tbody>
      </table>
      <button className="btn ghost" style={{ marginTop: 6 }} onClick={() => setApproaches([...approaches, { id: `A${approaches.length + 1}`, name: "", bearing: 45, lanes: 1 }])}>+ approach</button>
      <div className="muted small" style={{ marginTop: 4 }}>Opposite approaches (bearings within 45° of the same axis) share a phase, so conflicting greens are impossible.</div>

      <div className="row gap wrap" style={{ marginTop: 12 }}>
        <label className="field-inline">Mode
          <select value={mode} onChange={(e) => setMode(e.target.value as "control" | "mirror")}>
            <option value="control">control – we run the signal</option>
            <option value="mirror">mirror – read a live external signal (advisory)</option>
          </select>
        </label>
        {(["green", "yellow", "all_red", "min_green", "max_green"] as const).map((k) => (
          <label key={k} className="field-inline">{k.replace("_", " ")}
            <input type="number" value={plan[k]} style={{ width: 60 }} onChange={(e) => setPlan({ ...plan, [k]: Number(e.target.value) })} />
          </label>
        ))}
        <label className="field-inline">Corridor radius (m) <input type="number" value={radius} style={{ width: 70 }} onChange={(e) => setRadius(Number(e.target.value))} /></label>
      </div>

      <div className="two-mini">
        <div>
          <div className="card-title small-title" style={{ marginTop: 12 }}>Output – signal to drive</div>
          <select value={String(driver.type)} onChange={(e) => setDriver({ type: e.target.value })}>
            <option value="virtual">Virtual (dashboard only)</option>
            <option value="serial">Arduino / ESP32 over USB serial</option>
            <option value="http">HTTP device / gateway (ESP32 Wi-Fi)</option>
            <option value="mqtt">MQTT broker</option>
          </select>
          {driver.type === "serial" && (
            <div className="row gap wrap" style={{ marginTop: 6 }}>
              <label className="field-inline">Port
                <input list="ports" value={String(driver.port ?? "")} onChange={(e) => setD("port", e.target.value)} placeholder="COM3 or /dev/ttyUSB0" />
                <datalist id="ports">{ports?.ports.map((p) => <option key={p.device} value={p.device}>{p.description}</option>)}</datalist>
              </label>
              <label className="field-inline">Baud <input value={String(driver.baud ?? 115200)} style={{ width: 80 }} onChange={(e) => setD("baud", Number(e.target.value))} /></label>
            </div>
          )}
          {driver.type === "http" && (
            <div className="row gap wrap" style={{ marginTop: 6 }}>
              <label className="field-inline">URL <input value={String(driver.url ?? "")} placeholder="http://192.168.1.60/state" onChange={(e) => setD("url", e.target.value)} /></label>
              <label className="field-inline">Format <select value={String(driver.format ?? "simple")} onChange={(e) => setD("format", e.target.value)}><option value="simple">simple JSON</option><option value="spat">J2735 SPaT JSON</option></select></label>
            </div>
          )}
          {driver.type === "mqtt" && (
            <div className="row gap wrap" style={{ marginTop: 6 }}>
              <label className="field-inline">Host <input value={String(driver.host ?? "localhost")} onChange={(e) => setD("host", e.target.value)} style={{ width: 120 }} /></label>
              <label className="field-inline">Port <input value={String(driver.port ?? 1883)} onChange={(e) => setD("port", Number(e.target.value))} style={{ width: 60 }} /></label>
              <label className="field-inline">Topic <input value={String(driver.topic ?? "")} placeholder="resqroute/signals/J1" onChange={(e) => setD("topic", e.target.value)} /></label>
              <label className="field-inline">Format <select value={String(driver.format ?? "simple")} onChange={(e) => setD("format", e.target.value)}><option value="simple">simple JSON</option><option value="spat">J2735 SPaT JSON</option></select></label>
            </div>
          )}
        </div>
        <div>
          <div className="card-title small-title" style={{ marginTop: 12 }}>Input – live signal data (open formats)</div>
          <select value={String(source.type)} onChange={(e) => setSource({ type: e.target.value, poll_s: 1 })}>
            <option value="none">None</option>
            <option value="spat">SAE J2735 SPaT JSON (HTTP / file)</option>
            <option value="sensorthings">OGC SensorThings API</option>
            <option value="json">Simple JSON (HTTP / file)</option>
            <option value="mqtt">MQTT (SPaT / JSON / SensorThings)</option>
          </select>
          {source.type !== "none" && source.type !== "mqtt" && source.type !== "sensorthings" && (
            <label className="field">URL or file path
              <input value={String(source.url ?? "")} placeholder="https://… or C:\\data\\spat.json" onChange={(e) => setS("url", e.target.value)} />
            </label>
          )}
          {source.type === "spat" && (
            <>
              <label className="field">Intersection id (optional) <input value={String(source.intersection_id ?? "")} onChange={(e) => setS("intersection_id", e.target.value || undefined)} /></label>
              {jsonField("group_map", '{"2": "N", "4": "E", "6": "S", "8": "W"}')}
            </>
          )}
          {source.type === "sensorthings" && (
            <>
              <label className="field">Base URL <input value={String(source.base_url ?? "")} placeholder="https://<server>/v1.1" onChange={(e) => setS("base_url", e.target.value)} /></label>
              {jsonField("datastreams", '{"N": 12345, "E": 12346}')}
              {jsonField("value_map", '{"0": "X", "1": "R", "3": "G", "4": "Y"}')}
            </>
          )}
          {source.type === "mqtt" && (
            <div className="row gap wrap">
              <label className="field-inline">Host <input value={String(source.host ?? "")} onChange={(e) => setS("host", e.target.value)} style={{ width: 120 }} /></label>
              <label className="field-inline">Port <input value={String(source.port ?? 1883)} onChange={(e) => setS("port", Number(e.target.value))} style={{ width: 60 }} /></label>
              <label className="field-inline">Topic <input value={String(source.topic ?? "")} onChange={(e) => setS("topic", e.target.value)} /></label>
              <label className="field-inline">Format <select value={String(source.format ?? "json")} onChange={(e) => setS("format", e.target.value)}><option value="json">simple JSON</option><option value="spat">SPaT</option><option value="sensorthings">SensorThings</option></select></label>
              {jsonField("group_map", '{"2": "N", "4": "E"}')}
            </div>
          )}
          {source.type !== "none" && source.type !== "mqtt" && (
            <div className="row gap" style={{ marginTop: 6 }}>
              <button className="btn ghost" onClick={testSource}>Test source</button>
              {test && <code className="small">{test.slice(0, 200)}</code>}
            </div>
          )}
        </div>
      </div>
      <div className="row gap" style={{ marginTop: 12 }}>
        <button className="btn" onClick={save}>Save junction</button>
        <button className="btn ghost" onClick={onCancel}>Cancel</button>
      </div>
      {error && <div className="error">{error}</div>}
    </div>
  );
}

// ----------------------------------------------------------------------- page
export default function FieldPage({ network }: { network: NetworkInfo }) {
  const snap = useField();
  const [sel, setSel] = useState<string | null>(null);
  const [editing, setEditing] = useState<"new" | "edit" | null>(null);
  const [picked, setPicked] = useState<[number, number] | null>(null);
  const [route, setRoute] = useState<string[]>([]);
  const [entry, setEntry] = useState("");
  const [speed, setSpeed] = useState(40);
  const [preview, setPreview] = useState<[number, number][]>([]);
  const [gpsOn, setGpsOn] = useState(false);
  const [gpsMsg, setGpsMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const trails = useRef<Record<string, [number, number][]>>({});
  const { data: cams, reload: reloadCams } = usePoll(() => api<VideoSourceInfo[]>("/api/video/sources"), 4000);

  useEffect(() => { if (!sel && snap?.junctions.length) setSel(snap.junctions[0].id); }, [snap, sel]);
  useEffect(() => {
    for (const a of snap?.ambulances ?? []) {
      const t = (trails.current[a.id] ??= []);
      const last = t[t.length - 1];
      if (!last || last[0] !== a.lat || last[1] !== a.lon) t.push([a.lat, a.lon]);
      if (t.length > 300) t.shift();
    }
  }, [snap]);
  useEffect(() => {
    if (!gpsOn) return;
    if (!navigator.geolocation || !window.isSecureContext) {
      setGpsMsg("This browser only shares GPS on https:// or http://localhost. Open the dashboard on the ambulance phone via an HTTPS tunnel, or use the simulated drive.");
      setGpsOn(false);
      return;
    }
    const id = navigator.geolocation.watchPosition(
      (p) => {
        setGpsMsg(`GPS ${p.coords.latitude.toFixed(5)}, ${p.coords.longitude.toFixed(5)} ±${Math.round(p.coords.accuracy)} m`);
        post("/api/field/ambulances/DRIVER/position", {
          lat: p.coords.latitude, lon: p.coords.longitude,
          speed: p.coords.speed ?? undefined, heading: p.coords.heading ?? undefined,
        }).catch(() => undefined);
      },
      (e) => setGpsMsg(e.message), { enableHighAccuracy: true, maximumAge: 1000 });
    return () => navigator.geolocation.clearWatch(id);
  }, [gpsOn]);

  if (!snap) return <div className="card muted">Connecting to the signal manager…</div>;
  const j = snap.junctions.find((x) => x.id === sel) ?? null;
  const act = async (fn: () => Promise<unknown>) => { setError(null); try { await fn(); } catch (e) { setError((e as Error).message); } };
  const attached = (cams ?? []).filter((c) => c.attach && (c.attach as Record<string, string>).field === j?.id);

  const simulate = () => act(async () => {
    const r = await post<{ path: [number, number][] }>("/api/field/ambulances/SIM-AMB/simulate",
      { junctions: route.length ? route : j ? [j.id] : [], entry_approach: entry || undefined, speed_kmh: speed });
    setPreview(r.path);
    trails.current["SIM-AMB"] = [];
  });

  return (
    <div className="two-col">
      <div className="col">
        <div className="card list">
          <div className="row between">
            <div className="card-title">Real-time signals</div>
            <button className="btn" onClick={() => { setEditing("new"); setPicked(null); }}>+ New</button>
          </div>
          {snap.junctions.map((x) => (
            <div key={x.id} className={`list-item ${x.id === sel ? "active" : ""}`} onClick={() => { setSel(x.id); setEditing(null); }}>
              <div className="row between"><b>{x.id} · {x.name}</b><span className="mode" style={{ background: MODE_COLOR[x.mode_now] }}>{x.mode_now}</span></div>
              <div className="muted small">
                {x.mode === "mirror" ? `mirrors ${String(x.source.type)}` : `drives ${String(x.driver.type)}`} ·{" "}
                {Object.entries(x.states).map(([a, s]) => <span key={a} style={{ color: LAMP[s as Lamp] }}>{a}●</span>)}
              </div>
            </div>
          ))}
        </div>
        <div className="card">
          <div className="card-title">Ambulance corridor</div>
          <p className="muted small">Any ambulance GPS feed (POST /api/field/ambulances/&lt;id&gt;/position) gets green lights along its way. The simulated ambulances on the Live map are followed too{snap.follow_sim ? "" : " (off)"}.</p>
          <label className="toggle"><input type="checkbox" checked={snap.follow_sim} onChange={(e) => act(() => post("/api/field/follow-sim", { enabled: e.target.checked }))} /> follow simulated ambulances</label>
          <div className="card-title small-title" style={{ marginTop: 10 }}>Simulated drive</div>
          <div className="muted small">Click junctions in driving order:</div>
          <div className="row gap wrap" style={{ margin: "6px 0" }}>
            {snap.junctions.map((x) => (
              <button key={x.id} className={`btn ${route.includes(x.id) ? "" : "ghost"}`}
                onClick={() => setRoute(route.includes(x.id) ? route.filter((r) => r !== x.id) : [...route, x.id])}>
                {route.includes(x.id) ? `${route.indexOf(x.id) + 1}. ` : ""}{x.id}
              </button>
            ))}
          </div>
          <div className="row gap wrap">
            <label className="field-inline">Enter from
              <select value={entry} onChange={(e) => setEntry(e.target.value)}>
                <option value="">auto</option>
                {(snap.junctions.find((x) => x.id === (route[0] ?? sel))?.approaches ?? []).map((a) => <option key={a.id} value={a.id}>{a.id} {a.name}</option>)}
              </select>
            </label>
            <label className="field-inline">Speed <input type="number" value={speed} style={{ width: 60 }} onChange={(e) => setSpeed(Number(e.target.value))} /> km/h</label>
            <button className="btn" onClick={simulate}>Drive</button>
            <button className="btn ghost" onClick={() => act(() => api("/api/field/ambulances/SIM-AMB/simulate", { method: "DELETE" }))}>Stop</button>
          </div>
          <div className="card-title small-title" style={{ marginTop: 10 }}>Driver mode (this device's GPS)</div>
          <label className="toggle"><input type="checkbox" checked={gpsOn} onChange={(e) => setGpsOn(e.target.checked)} /> send my location as ambulance “DRIVER”</label>
          {gpsMsg && <div className="muted small">{gpsMsg}</div>}
        </div>
      </div>

      <div className="col">
        <CorridorMap snap={snap} selected={sel} onSelect={(id) => { setSel(id); setEditing(null); }}
          onPick={editing ? setPicked : undefined} pathPreview={preview} trails={trails.current} />
        {error && <div className="error">{error}</div>}
        {editing ? (
          <JunctionForm key={editing === "new" ? "new" : sel ?? ""} initial={editing === "edit" ? j : null} picked={picked}
            onSaved={(id) => { setSel(id); setEditing(null); }} onCancel={() => setEditing(null)} />
        ) : j && (
          <>
            <div className="card" style={{ marginTop: 12 }}>
              <div className="row between wrap gap">
                <div>
                  <div className="card-title" style={{ marginBottom: 2 }}>{j.id} · {j.name}</div>
                  <div className="muted small">{j.lat.toFixed(5)}, {j.lon.toFixed(5)} · {j.mode === "mirror" ? `read-only mirror of ${String(j.source.type)}` : `controls a ${String(j.driver.type)} signal`} · conflicting greens: {j.violations}</div>
                </div>
                <div className="row gap">
                  <button className="btn ghost" onClick={() => setEditing("edit")}>Edit</button>
                  <button className="btn danger" onClick={() => act(async () => { await api(`/api/field/junctions/${j.id}`, { method: "DELETE" }); setSel(null); })}>Delete</button>
                </div>
              </div>
              <div className="junction-layout">
                <JunctionGraphic j={j} />
                <div className="col">
                  {j.mode === "control" && (
                    <div>
                      <div className="card-title small-title">Emergency override</div>
                      <div className="row gap wrap">
                        {j.approaches.map((a) => (
                          <button key={a.id} className="btn" style={{ background: "#8e4ec6" }} onClick={() => act(() => post(`/api/field/junctions/${j.id}/priority`, { approach: a.id }))}>
                            Green for {a.id}
                          </button>
                        ))}
                        {j.active && <button className="btn ghost" onClick={() => act(() => post(`/api/field/junctions/${j.id}/release`))}>Release</button>}
                      </div>
                    </div>
                  )}
                  <div className="stats" style={{ marginTop: 10 }}>
                    <Stat label="Mode" value={<span style={{ color: MODE_COLOR[j.mode_now] }}>{j.mode_now}</span>} sub={j.stage ? `${j.stage} · group ${(j.current_group ?? 0) + 1} · ${j.elapsed}s` : "external source"} />
                    <Stat label="Priority" value={j.active ? `${j.active.approach} (${j.active.trigger})` : "–"}
                      sub={j.active ? `${j.active.status}${j.active.hold_s != null ? ` · held ${j.active.hold_s}s` : ""}` : j.pending.length ? `${j.pending.length} queued` : "no ambulance"} />
                    <Stat label="Output" value={String(j.driver_status.type)} sub={j.driver_status.error ? <span className="bad">{j.driver_status.error}</span> : `${j.driver_status.sent} messages`} />
                    {j.source_status && (
                      <Stat label="Live input" value={j.source_status.type} sub={j.source_status.error ? <span className="bad">{j.source_status.error}</span> : `${j.source_status.fresh ? "fresh" : "stale"} · ${j.source_status.messages} updates`} />
                    )}
                  </div>
                  <div className="muted small">
                    Open-format outputs: <a href={`${API_BASE}/api/field/junctions/${j.id}/spat`} target="_blank" rel="noreferrer">SPaT JSON</a> ·{" "}
                    <a href={`${API_BASE}/api/field/junctions/${j.id}/state`} target="_blank" rel="noreferrer">simple JSON</a>
                    {Object.keys(j.compensation).length > 0 && ` · owed compensation: ${Object.entries(j.compensation).map(([g, s]) => `group ${Number(g) + 1} +${s}s`).join(", ")}`}
                  </div>
                </div>
              </div>
            </div>

            <div className="card">
              <div className="card-title">Cameras on this junction</div>
              {attached.length === 0 && <div className="muted small">None. On <b>Video analytics</b>, open a camera (webcam, phone stream or video) and attach it to “{j.id} → approach”. When it recognises an ambulance in its zone the approach turns green; when the ambulance crosses the stop line the junction goes back to normal.</div>}
              {attached.map((c) => (
                <div key={c.id} className="kv">
                  <b>{c.name}</b> → approach {(c.attach as Record<string, string>).approach} <span className="muted small">{c.status} · {c.stats ? `${c.stats.vehicles} vehicles, queue ${c.stats.queue}${c.stats.ambulances ? `, ${c.stats.ambulances} ambulance` : ""}` : ""}</span>
                </div>
              ))}
              <button className="btn ghost" style={{ marginTop: 6 }} onClick={reloadCams}>Refresh</button>
            </div>

            <div className="card">
              <div className="card-title">Junction log</div>
              <ul className="timeline">
                {j.events.slice().reverse().map((e, i) => (
                  <li key={i}><span className="ev-t">{new Date(e.wall * 1000).toLocaleTimeString()}</span>
                    <span className={e.type.startsWith("PRIORITY") ? "ev-priority" : e.type.startsWith("RECOVERY") ? "ev-recovery" : ""}>{EVENT_TEXT[e.type]?.(e) ?? e.type}</span></li>
                ))}
                {j.events.length === 0 && <li className="muted">No events yet – try “Green for …”, a simulated drive, or a camera.</li>}
              </ul>
            </div>
          </>
        )}
        <div className="muted small" style={{ marginTop: 8 }}>
          Physical demo signals: see <code>hardware/</code> (Arduino serial / ESP32 Wi-Fi sketches with a conflict monitor and fail-safe flashing amber).
          Live public signal feeds are usually read-only – use <b>mirror</b> mode; real street signals need a certified controller and the traffic authority's permission.
          Map tiles {network.source === "osm" ? "and roads are" : "are"} © OpenStreetMap contributors.
        </div>
      </div>
    </div>
  );
}
