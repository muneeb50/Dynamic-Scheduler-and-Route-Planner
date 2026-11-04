import { memo, useState, type ReactNode } from "react";
import { SimEvent, SimSnapshot, SimStatus, STATUS_LABEL, fmtTime, post } from "../api";
import { LinkState } from "../hooks/useSimulation";

export function StatusPill({ status }: { status: string }) {
  return <span className={`pill pill-${status.toLowerCase()}`}>{STATUS_LABEL[status] ?? status}</span>;
}

export function LinkBadge({ link }: { link: LinkState }) {
  const label = { connecting: "Connecting…", live: "Live", stale: "Stale data", offline: "Reconnecting" }[link];
  return <span className={`link link-${link}`}>{label}</span>;
}

export function ControlPanel({ snap, onChanged }: { snap: SimSnapshot | null; onChanged?: (s: SimStatus) => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const send = async (body: Record<string, unknown>) => {
    setBusy(true);
    setError(null);
    try {
      const s = await post<SimStatus>("/api/simulation/control", body);
      onChanged?.(s);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  if (!snap) return <div className="card muted">Waiting for simulation…</div>;
  return (
    <div className="card">
      <div className="card-title">Simulation control</div>
      <div className="row gap wrap">
        <button className="btn" disabled={busy} onClick={() => send({ action: snap.running ? "pause" : "resume" })}>
          {snap.running ? "Pause" : "Resume"}
        </button>
        <label className="field-inline">
          Speed
          <select value={snap.speed} disabled={busy} onChange={(e) => send({ speed: Number(e.target.value) })}>
            {Array.from(new Set([1, 2, 3, 5, 10, 20, snap.speed])).sort((a, b) => a - b).map((s) => (
              <option key={s} value={s}>{s}×</option>
            ))}
          </select>
        </label>
        <label className="field-inline">
          Demand
          <select value={snap.demand} disabled={busy} onChange={(e) => send({ action: "reset", demand: e.target.value })}>
            {["off", "low", "medium", "high", "peak"].map((d) => (
              <option key={d}>{d}</option>
            ))}
          </select>
        </label>
        <label className="field-inline">
          Random emergencies
          <select value={snap.auto_emergencies_per_hour ?? 0} disabled={busy}
            onChange={(e) => send({ auto_emergencies_per_hour: Number(e.target.value) })}>
            {Array.from(new Set([0, 4, 12, 30, 60, 120, snap.auto_emergencies_per_hour ?? 0])).sort((a, b) => a - b).map((r) => (
              <option key={r} value={r}>{r === 0 ? "off" : `${r}/hour`}</option>
            ))}
          </select>
        </label>
      </div>
      <div className="row gap wrap" style={{ marginTop: 10 }}>
        <label className="toggle">
          <input type="checkbox" checked={snap.priority_enabled} disabled={busy}
            onChange={(e) => send({ priority_enabled: e.target.checked })} />
          Emergency priority
        </label>
        <label className="toggle">
          <input type="checkbox" checked={snap.queue_source === "cv"} disabled={busy}
            onChange={(e) => send({ queue_source: e.target.checked ? "cv" : "sim" })} />
          Queues from computer vision
        </label>
        <button className="btn ghost" disabled={busy} onClick={() => send({ action: "reset" })}>Reset run</button>
      </div>
      {error && <div className="error">{error}</div>}
    </div>
  );
}

const EVENT_TEXT: Record<string, (e: SimEvent) => string> = {
  BOOKING_CREATED: (e) => `Booking ${e.journey_id} created (pickup ${e.pickup_node})`,
  AMBULANCE_ASSIGNED: (e) => `${e.ambulance_id} assigned to ${e.journey_id}, ETA ${fmtTime(e.eta_s as number)}`,
  NO_AMBULANCE_AVAILABLE: (e) => `No ambulance available for ${e.journey_id} – queued`,
  LEG_STARTED: (e) => `${e.ambulance_id} started ${String(e.leg).replace("_", " ")} (${e.length_m} m)`,
  ARRIVED_AT_PICKUP: (e) => `${e.ambulance_id} reached pickup for ${e.journey_id}`,
  ARRIVED_AT_HOSPITAL: (e) => `${e.ambulance_id} delivered ${e.journey_id} to hospital`,
  PRIORITY_REQUESTED: (e) =>
    e.source === "camera" ? `📷 Camera saw an ambulance – green requested at ${N(e.node)} (${e.queue_ahead} vehicles ahead per lane)`
      : e.source === "manual" ? `Operator requested green at ${N(e.node)}`
        : `Priority requested at ${N(e.node)}: ETA ${e.eta_s}s, ${e.queue_ahead} vehicles ahead, lead ${e.lead_time_s}s`,
  PRIORITY_GRANTED: (e) => `Green granted at ${N(e.node)}`,
  PRIORITY_RELEASED: (e) => `Ambulance cleared ${N(e.node)} – priority released (held ${e.hold_time_s ?? "–"}s)`,
  PRIORITY_TIMEOUT: (e) => `Priority at ${N(e.node)} timed out`,
  PRIORITY_DEFERRED: (e) => `Priority at ${N(e.node)} deferred: ${e.reason}`,
  RECOVERY_STARTED: (e) => `Recovery started at ${N(e.node)}`,
  RECOVERY_COMPLETED: (e) => `Recovery at ${N(e.node)} completed in ${e.duration_s}s`,
  RECOVERY_TIMEOUT: (e) => `Recovery at ${N(e.node)} hit its time limit (degraded)`,
  RECOVERY_SUPERSEDED: (e) => `Recovery at ${N(e.node)} superseded by a new priority request`,
  BOOKING_CANCELLED: (e) => `Booking ${e.journey_id} cancelled`,
  JOURNEY_FAILED: (e) => `Journey ${e.journey_id} failed: ${e.reason}`,
  SAFETY_VIOLATION: (e) => `SAFETY VIOLATION at ${N(e.node)}`,
  CAMERA_EMERGENCY_VEHICLE: (e) => `Camera ${e.source_name}: ${e.class} recognised (score ${e.conf}${Array.isArray(e.reasons) && e.reasons.length ? `; ${(e.reasons as string[]).join(", ")}` : ""})`,
  CAMERA_AMBULANCE_IN_ZONE: (e) => `Camera ${e.source_name}: ambulance entering the approach zone`,
  CAMERA_AMBULANCE_PASSED: (e) => `Camera ${e.source_name}: ambulance crossed the stop line`,
  FIELD_PRIORITY_REQUESTED: (e) => `Signal ${e.junction}: green requested for ${e.approach} (${e.trigger})`,
  FIELD_PRIORITY_GRANTED: (e) => `Signal ${e.junction}: ${e.approach} is green for the ambulance`,
  FIELD_PRIORITY_RELEASED: (e) => `Signal ${e.junction}: priority released (${String(e.reason).toLowerCase()}), back to normal`,
  FIELD_PRIORITY_ADVISORY: (e) => `Signal ${e.junction} (read-only): should give green to ${e.approach}`,
  AMBULANCE_AVAILABLE: (e) => `${e.ambulance_id} back at base`,
  MAP_LOADED: (e) => `Map switched to ${e.name} (${e.nodes ?? "?"} junctions, ${e.signals ?? "?"} signals)`,
  TEST_AMBULANCE_STARTED: (e) => `Test ambulance ${e.test_id} heading for ${N(e.node)} (${e.length_m} m)`,
  TEST_AMBULANCE_FINISHED: (e) => `Test ambulance ${e.test_id} finished after ${fmtTime(e.duration_s as number)}`,
};

let NAMES: Record<string, string> = {};
const N = (id: unknown) => NAMES[String(id)] ?? String(id);

export const EventLog = memo(function EventLog({ events, max = 60, names }: { events: SimEvent[]; max?: number; names?: Record<string, string> }) {
  if (names) NAMES = names;
  const shown = events.filter((e) => EVENT_TEXT[e.type]).slice(0, max);
  return (
    <div className="card events">
      <div className="card-title">Event log</div>
      {shown.length === 0 && <div className="muted">No events yet. Request an ambulance to begin.</div>}
      <ul>
        {shown.map((e, i) => (
          <li key={i} className={`ev ev-${e.type.split("_")[0].toLowerCase()}`}>
            <span className="ev-t">{fmtTime(e.t)}</span>
            <span>{EVENT_TEXT[e.type]?.(e) ?? e.type}</span>
          </li>
        ))}
      </ul>
    </div>
  );
});

export function Stat({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub && <div className="stat-sub">{sub}</div>}
    </div>
  );
}
