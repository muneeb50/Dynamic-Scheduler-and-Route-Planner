import { useMemo, useState } from "react";
import { NetworkInfo, SimEvent, SimSnapshot, fmtTime } from "../api";
import LiveMap from "../components/LiveMap";
import MapAreaPanel from "../components/MapAreaPanel";
import { ControlPanel, EventLog, LinkBadge, Stat, StatusPill } from "../components/Panels";
import { LinkState } from "../hooks/useSimulation";

interface Props {
  network: NetworkInfo;
  snap: SimSnapshot | null;
  link: LinkState;
  events: SimEvent[];
  onOpenIntersection: (id: string) => void;
}

export default function LivePage({ network, snap, link, events, onOpenIntersection }: Props) {
  const halted = snap ? snap.vehicles.filter((v) => v[3] === 0 && v[2] < 0.5).length : 0;
  const [viewBox, setViewBox] = useState<number[] | null>(null);
  const [preview, setPreview] = useState<number[] | null>(null);
  const names = useMemo(() => Object.fromEntries(network.intersections.map((i) => [i.id, i.name])), [network]);
  const nm = (id: string) => names[id] ?? id;
  return (
    <div className="live-layout">
      <div className="map-wrap">
        <LiveMap network={network} snap={snap} onSelectNode={onOpenIntersection} onBounds={setViewBox} previewBox={preview} />
        <div className="legend">
          <span><i className="dot" style={{ background: "#3e63dd" }} /> moving car</span>
          <span><i className="dot" style={{ background: "#f76b15" }} /> stopped car</span>
          <span><i className="bar" style={{ background: "#30a46c" }} />/<i className="bar" style={{ background: "#f5d90a" }} />/<i className="bar" style={{ background: "#e5484d" }} /> approach signal</span>
          <span><i className="ring" style={{ borderColor: "#8e4ec6" }} /> priority</span>
          <span><i className="ring" style={{ borderColor: "#f76b15" }} /> recovery</span>
          <span className="muted">zoom in to see each approach's light</span>
        </div>
      </div>
      <aside className="side">
        <div className="stats">
          <Stat label="Sim time" value={fmtTime(snap?.t)} sub={<LinkBadge link={link} />} />
          <Stat label="Vehicles" value={snap?.vehicle_count?.toLocaleString() ?? "–"} sub={`${halted.toLocaleString()} stopped · ${network.signals} signals`} />
          <Stat label="Active journeys" value={snap?.journeys.length ?? 0} sub={`${snap?.priority.length ?? 0} priority · ${snap?.recovery.length ?? 0} recovering`} />
          <Stat label="Safety violations" value={<span className={snap?.safety_violations ? "bad" : "good"}>{snap?.safety_violations ?? 0}</span>} sub={`${snap?.engine ?? ""} engine`} />
        </div>
        <ControlPanel snap={snap} />
        <MapAreaPanel network={network} snap={snap} viewBox={viewBox} onPreview={setPreview} />

        <div className="card">
          <div className="card-title">Active journeys</div>
          {snap?.journeys.length ? (
            <table className="tbl">
              <thead><tr><th>Booking</th><th>Status</th><th>Amb.</th><th>ETA</th></tr></thead>
              <tbody>
                {snap.journeys.map((j) => (
                  <tr key={j.id}>
                    <td>{j.id}</td>
                    <td><StatusPill status={j.status} /></td>
                    <td>{j.ambulance_id ?? "–"}</td>
                    <td>{fmtTime(j.eta_s)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="muted">None. Use “Request ambulance” or the mobile app.</div>
          )}
        </div>

        {(snap?.cameras?.length ?? 0) > 0 && (
          <div className="card">
            <div className="card-title">Live cameras (YOLO / OpenCV)</div>
            {snap!.cameras!.map((c) => (
              <div key={c.id} className="kv">
                <b>{c.name}</b> <span className="muted small">{c.status} · {c.proc_fps} fps</span>
                <div className="muted small">
                  {c.vehicles ?? "–"} vehicles · queue {c.queue ?? "–"} · {c.flow_per_min ?? "–"}/min · {c.congestion ?? "–"}
                  {c.attach ? ` · feeds ${c.attach.approach}` : ""}
                </div>
              </div>
            ))}
          </div>
        )}
        {(snap?.priority.length ?? 0) > 0 && (
          <div className="card accent-purple">
            <div className="card-title">Emergency priority</div>
            {snap!.priority.map((p) => (
              <div key={p.id} className="kv clickable" onClick={() => onOpenIntersection(p.node)}>
                <b>{nm(p.node)}</b> · {p.status}{p.source && p.source !== "route" ? ` · ${p.source === "camera" ? "📷 camera" : "operator"}` : ""}
                <div className="muted small">
                  requested at {fmtTime(p.requested_at)} · {p.source === "route" || !p.source ? `ETA ${p.eta_s}s · ` : ""}{p.queue_ahead} ahead · lead {p.lead_time_s}s
                </div>
              </div>
            ))}
          </div>
        )}
        {(snap?.recovery.length ?? 0) > 0 && (
          <div className="card accent-orange">
            <div className="card-title">Traffic recovery</div>
            {snap!.recovery.map((r) => (
              <div key={r.id} className="kv clickable" onClick={() => onOpenIntersection(r.node)}>
                <b>{nm(r.node)}</b> · running {fmtTime((snap?.t ?? 0) - r.started_at)}
                <div className="muted small">
                  peak backlog {Math.max(0, ...Object.values(r.peak_backlog)).toFixed(0)} veh ·
                  {" "}last green {r.timing_adjustments.at(-1)?.green_s ?? "–"}s
                </div>
              </div>
            ))}
          </div>
        )}
        <EventLog events={events} names={names} />
      </aside>
    </div>
  );
}
