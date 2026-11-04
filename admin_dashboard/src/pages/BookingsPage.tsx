import { useState } from "react";
import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Booking, JourneyMetrics, NetworkInfo, SimSnapshot, api, fmtTime, post } from "../api";
import LiveMap from "../components/LiveMap";
import { Stat, StatusPill } from "../components/Panels";
import { usePoll } from "../hooks/useSimulation";

interface Analytics {
  booking_id: number; status: string; metrics: JourneyMetrics | null; priority_enabled: boolean | null;
  priority_events: { type: string; sim_time: number; node: string; eta_s?: number; queue_ahead?: number; hold_time_s?: number }[];
  recoveries: { node: string; status: string; duration_s: number | null }[];
}
interface EventRow { source: string; type: string; sim_time: number; intersection?: string; payload: Record<string, unknown> }

const FINAL = ["COMPLETED", "CANCELLED", "FAILED", "REJECTED"];

export default function BookingsPage({ network, snap }: { network: NetworkInfo; snap: SimSnapshot | null }) {
  const [sel, setSel] = useState<number | null>(null);
  const { data: bookings, reload } = usePoll(() => api<Booking[]>("/api/bookings?limit=100"), 3000);
  const current = bookings?.find((b) => b.id === sel) ?? null;
  const { data: an } = usePoll<Analytics | null>(() => (sel ? api<Analytics>(`/api/analytics/${sel}`) : Promise.resolve(null)), 4000, [sel]);
  const { data: evs } = usePoll<EventRow[] | null>(() => (sel ? api<EventRow[]>(`/api/bookings/${sel}/events`) : Promise.resolve(null)), 4000, [sel]);

  const act = async (path: string) => {
    try {
      await post(path);
      reload();
    } catch (e) {
      alert((e as Error).message);
    }
  };

  return (
    <div className="two-col">
      <div className="card list">
        <div className="card-title">Bookings</div>
        {!bookings?.length && <div className="muted">No bookings yet.</div>}
        {bookings?.map((b) => (
          <div key={b.id} className={`list-item ${b.id === sel ? "active" : ""}`} onClick={() => setSel(b.id)}>
            <div className="row between">
              <b>#{b.id} {b.patient_name}</b>
              <StatusPill status={b.status} />
            </div>
            <div className="muted small">
              → {b.hospital_name} · amb {b.ambulance_id ?? "–"} · run {b.run_id}
              {b.eta_s !== null && !FINAL.includes(b.status) ? ` · ETA ${fmtTime(b.eta_s)}` : ""}
            </div>
          </div>
        ))}
      </div>
      <div className="col">
        {!current && <div className="card muted">Select a booking to see its journey, signal priority and recovery history.</div>}
        {current && (
          <>
            <div className="card">
              <div className="row between">
                <div className="card-title">Booking #{current.id} · {current.patient_name}</div>
                <div className="row gap">
                  {current.status === "WAITING_FOR_AMBULANCE" && (
                    <button className="btn" onClick={() => act(`/api/bookings/${current.id}/assign`)}>Assign now</button>
                  )}
                  {!FINAL.includes(current.status) && (
                    <button className="btn danger" onClick={() => act(`/api/bookings/${current.id}/cancel`)}>Cancel</button>
                  )}
                </div>
              </div>
              <div className="row gap wrap">
                <StatusPill status={current.status} />
                <span className="muted small">pickup node {current.pickup_node} → {current.hospital_name}</span>
                {current.failure_reason && <span className="error small">{current.failure_reason}</span>}
              </div>
              {current.live && (
                <div style={{ height: 260, marginTop: 10 }}>
                  <LiveMap network={network} snap={snap} focusJourney={current.live.id} showVehicles compact />
                </div>
              )}
            </div>
            {an?.metrics && <MetricsCard m={an.metrics} priority={an.priority_enabled} />}
            <div className="card">
              <div className="card-title">Journey timeline</div>
              <ul className="timeline">
                {evs?.map((e, i) => (
                  <li key={i} className={e.source}>
                    <span className="ev-t">{fmtTime(e.sim_time)}</span>
                    <b>{e.type.replace(/_/g, " ").toLowerCase()}</b>
                    {e.intersection && <span className="muted"> @ {e.intersection}</span>}
                  </li>
                ))}
              </ul>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function MetricsCard({ m, priority }: { m: JourneyMetrics; priority: boolean | null }) {
  const bars = m.intersections.map((d) => ({ name: d.node, delay: d.delay_s, priority: d.priority }));
  return (
    <div className="card">
      <div className="card-title">Journey metrics {priority === null ? "" : priority ? "(priority ON)" : "(priority OFF – baseline)"}</div>
      <div className="stats">
        <Stat label="Dispatch → hospital" value={fmtTime(m.total_time_s)} sub={`free-flow ${fmtTime(m.free_flow_s)} + dwell`} />
        <Stat label="To pickup / hospital" value={`${fmtTime(m.to_pickup_s)} / ${fmtTime(m.to_hospital_s)}`} sub={`${(m.distance_m / 1000).toFixed(2)} km`} />
        <Stat label="Signal delay" value={`${m.signal_delay_s}s`} sub={`${m.stops} stops · ${m.waiting_s}s stopped`} />
        <Stat label="Priority / recovery" value={`${m.priority_granted}/${m.priority_requests}`} sub={`${m.recoveries_completed} recovered · ${m.recoveries_timeout} timeouts · mean ${m.mean_recovery_s ?? "–"}s`} />
      </div>
      {bars.length > 0 && (
        <ResponsiveContainer width="100%" height={170}>
          <BarChart data={bars}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--grid)" />
            <XAxis dataKey="name" stroke="var(--muted)" fontSize={11} />
            <YAxis stroke="var(--muted)" fontSize={11} unit="s" />
            <Tooltip contentStyle={{ background: "var(--panel)", border: "1px solid var(--border)" }} />
            <Bar dataKey="delay" name="delay at junction (s)">
              {bars.map((b, i) => <Cell key={i} fill={b.priority ? "#8e4ec6" : "#8b8d98"} />)}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      )}
      <div className="muted small">Purple bars: junctions where priority was granted.</div>
    </div>
  );
}
