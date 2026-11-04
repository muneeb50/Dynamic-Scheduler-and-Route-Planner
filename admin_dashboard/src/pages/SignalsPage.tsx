import { useMemo, useState } from "react";
import { NetworkInfo, SimSnapshot, fmtTime } from "../api";
import LiveMap from "../components/LiveMap";
import { Stat } from "../components/Panels";
import FieldPage from "./FieldPage";

/** Simulated traffic signals of the loaded map, how emergencies change them, and (advanced) LED / external signals. */
export default function SignalsPage({ network, snap, onOpenIntersection }: {
  network: NetworkInfo; snap: SimSnapshot | null; onOpenIntersection: (id: string) => void;
}) {
  const [advanced, setAdvanced] = useState(false);
  const names = useMemo(() => Object.fromEntries(network.intersections.map((i) => [i.id, i.name])), [network]);
  const sig = snap?.signals ?? {};
  const modes = Object.values(sig).reduce((acc, s) => ({ ...acc, [s.mode]: (acc[s.mode] ?? 0) + 1 }), {} as Record<string, number>);
  const greenShare = (() => {
    let g = 0, n = 0;
    for (const s of Object.values(sig)) for (const v of Object.values(s.approaches)) { n += 1; if (v === "G") g += 1; }
    return n ? Math.round((100 * g) / n) : 0;
  })();
  const osm = network.intersections.filter((i) => i.signal_source === "osm").length;
  const inferred = network.intersections.filter((i) => i.signal_source === "inferred").length;
  const cams = (snap?.cameras ?? []).filter((c) => c.attach?.node);
  const busiest = Object.entries(sig)
    .map(([id, s]) => ({ id, q: Object.values(s.queues).reduce((a, b) => a + b, 0), mode: s.mode }))
    .sort((a, b) => b.q - a.q).slice(0, 8);

  return (
    <div className="col">
      <div className="stats">
        <Stat label="Simulated signals" value={network.signals} sub={network.source === "osm" ? `${osm} mapped in OSM · ${inferred} inferred` : "demo network"} />
        <Stat label="Normal cycle" value={modes.NORMAL ?? 0} sub={`${greenShare}% of approaches green now`} />
        <Stat label="Emergency priority" value={<span style={{ color: "#8e4ec6" }}>{modes.PRIORITY ?? 0}</span>} sub={`${snap?.priority.length ?? 0} active requests`} />
        <Stat label="Recovering" value={<span style={{ color: "#f76b15" }}>{modes.RECOVERY ?? 0}</span>} sub="back to normal after an ambulance" />
        <Stat label="Conflicting greens" value={<span className={snap?.safety_violations ? "bad" : "good"}>{snap?.safety_violations ?? 0}</span>} sub="checked every step" />
      </div>
      <div className="signals-layout">
        <div className="card" style={{ padding: 0, overflow: "hidden" }}>
          <div style={{ height: 520 }}>
            <LiveMap network={network} snap={snap} onSelectNode={onOpenIntersection} showVehicles />
          </div>
        </div>
        <div className="col">
          <div className="card">
            <div className="card-title">How the simulated signals work</div>
            <ul className="small tight">
              <li>Every signal on the map runs its own simulated fixed-time cycle (green → amber → all-red → next phase), offset so junctions are not all in step. No real signal data is read.</li>
              <li><b>Ambulance approaching</b> (simulated booking, test ambulance, a camera that recognises an ambulance, or an operator): its approach is turned green early enough to flush the queue in front, never skipping minimum green, amber or all-red.</li>
              <li><b>After it passes</b>: priority is released and the junction <b>recovers</b> – longer greens for the approaches that were held, until queues are back to their pre-emergency level – then the normal cycle continues.</li>
            </ul>
            <div className="muted small">Click a junction on the map to open it, see its phases and queues, send a test ambulance through it or give a green manually.</div>
          </div>
          {(snap?.priority.length ?? 0) + (snap?.recovery.length ?? 0) > 0 && (
            <div className="card accent-purple">
              <div className="card-title">Emergency activity now</div>
              {snap!.priority.map((p) => (
                <div key={p.id} className="kv clickable" onClick={() => onOpenIntersection(p.node)}>
                  <b>{names[p.node] ?? p.node}</b> · {p.status === "GRANTED" ? "green for ambulance" : "switching to green"}
                  <span className="muted small"> · {p.source === "camera" ? "📷 camera" : p.source === "manual" ? "operator" : `ambulance ${p.vehicle_id.split("_")[0]}`}</span>
                </div>
              ))}
              {snap!.recovery.map((r) => (
                <div key={r.id} className="kv clickable" onClick={() => onOpenIntersection(r.node)}>
                  <b>{names[r.node] ?? r.node}</b> · recovering for {fmtTime((snap?.t ?? 0) - r.started_at)}
                </div>
              ))}
            </div>
          )}
          <div className="card">
            <div className="card-title">Busiest junctions</div>
            {busiest.map((b) => (
              <div key={b.id} className="kv clickable row between" onClick={() => onOpenIntersection(b.id)}>
                <span>{names[b.id] ?? b.id}</span>
                <span className="small"><span className={`mode mode-${b.mode.toLowerCase()}`}>{b.mode}</span> {b.q.toFixed(0)} queued</span>
              </div>
            ))}
          </div>
          <div className="card">
            <div className="card-title">Cameras driving simulated signals</div>
            {cams.length === 0 ? (
              <div className="muted small">None yet. On <b>Video AI</b>, open a webcam or video and attach it to a junction approach (target “Simulated junction”). When it recognises an ambulance in its zone, that approach turns green here; when the ambulance crosses the stop line, the junction recovers.</div>
            ) : cams.map((c) => (
              <div key={c.id} className="kv clickable" onClick={() => c.attach?.node && onOpenIntersection(c.attach.node)}>
                📷 <b>{c.name}</b> → {names[c.attach!.node!] ?? c.attach!.node} <span className="muted small">· {c.status} · {c.vehicles ?? 0} vehicles · queue {c.queue ?? 0}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
      <div className="card">
        <div className="row between">
          <div>
            <div className="card-title" style={{ marginBottom: 2 }}>Physical LED signal & external feeds (advanced, optional)</div>
            <div className="muted small">A stand-alone wall-clock signal for an Arduino / ESP32 LED model, or a read-only mirror of a SPaT / SensorThings / MQTT feed. Not needed for the simulation.</div>
          </div>
          <button className="btn ghost" onClick={() => setAdvanced(!advanced)}>{advanced ? "Hide" : "Show"}</button>
        </div>
      </div>
      {advanced && <FieldPage network={network} />}
    </div>
  );
}
