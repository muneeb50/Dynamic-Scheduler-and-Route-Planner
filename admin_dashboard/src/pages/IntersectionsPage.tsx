import { useMemo, useState } from "react";
import { CartesianGrid, Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { IntersectionInfo, NetworkInfo, PrioritySession, RecoveryState, SignalState, SimSnapshot, api, fmtTime, post } from "../api";
import { usePoll } from "../hooks/useSimulation";

const DIRS = ["north", "north-east", "east", "south-east", "south", "south-west", "west", "north-west"];
/** "from north" etc.: where traffic on this approach comes from. */
export function approachFrom(i: IntersectionInfo | undefined, a: string): string {
  const pts = i?.approaches[a];
  if (!pts || pts.length < 2) return "";
  const [p0, p1] = [pts[0], pts[pts.length - 1]];
  const dy = p1[0] - p0[0];
  const dx = (p1[1] - p0[1]) * Math.cos((p1[0] * Math.PI) / 180);
  const heading = (Math.atan2(dx, dy) * 180) / Math.PI;
  const from = (heading + 180 + 360) % 360;
  return `from ${DIRS[Math.round(from / 45) % 8]}`;
}

function SignalActions({ id, info, data }: { id: string; info?: IntersectionInfo; data: Status }) {
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [approach, setApproach] = useState("");
  const act = async (path: string, body: Record<string, unknown>, ok: string) => {
    setBusy(true);
    setMsg(null);
    try {
      await post(path, body);
      setMsg(ok);
    } catch (e) {
      setMsg((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const ext = data.external_priority ?? [];
  const label = (a: string) => `${approachFrom(info, a)} · ${data.approach_names[a] ?? a}`;
  return (
    <div className="card">
      <div className="card-title">Simulate an emergency here</div>
      <div className="row gap wrap">
        <label className="field-inline">Approach
          <select value={approach} onChange={(e) => setApproach(e.target.value)}>
            <option value="">longest approach</option>
            {Object.keys(data.approaches).map((a) => <option key={a} value={a}>{label(a)}</option>)}
          </select>
        </label>
        <button className="btn" disabled={busy} onClick={() => act(`/api/intersections/${id}/test-ambulance`, { approach: approach || undefined },
          "A test ambulance is on its way (shown in red on the Live map). Watch the request, green, release and recovery.")}>
          Send test ambulance through
        </button>
        <button className="btn ghost" disabled={busy || !approach} onClick={() => act(`/api/intersections/${id}/priority`, { approach, action: "request", hold_s: 45 },
          "Green requested for this approach (released after 45 s or when you press Release).")}>
          Green now (operator)
        </button>
        {ext.length > 0 && (
          <button className="btn ghost" disabled={busy} onClick={() => Promise.all(ext.map((x) => post(`/api/intersections/${id}/priority`, { approach: x.approach, action: "release" }))).then(() => setMsg("Released – the junction now recovers."), (e) => setMsg((e as Error).message))}>
            Release
          </button>
        )}
      </div>
      {ext.map((x) => (
        <div key={x.id} className="small" style={{ marginTop: 6 }}>
          {x.source === "camera" ? "📷 Camera" : "Operator"} priority on {label(x.approach)} · {x.status}
          {x.detail?.camera ? ` · ${String(x.detail.camera)}` : ""}
        </div>
      ))}
      {msg && <div className="muted small" style={{ marginTop: 6 }}>{msg}</div>}
      <div className="muted small" style={{ marginTop: 6 }}>
        Every change goes through minimum green → amber → all-red, exactly like route priority. A camera attached to an
        approach on the Video AI tab triggers the same green when it recognises an ambulance.
      </div>
    </div>
  );
}

const SERIES = ["#3e63dd", "#e5484d", "#30a46c", "#f76b15", "#8e4ec6", "#12a594"];

interface Status extends SignalState {
  id: string; name: string; approach_names: Record<string, string>;
  queue_history: ({ t: number } & Record<string, number>)[];
  priority_sessions: PrioritySession[]; active_recovery: RecoveryState | null; recent_recoveries: RecoveryState[];
  queue_source: string; signal_source?: string; roundabout?: boolean; external_priority?: PrioritySession[];
}

export default function IntersectionsPage({ network, snap, selected, onSelect }: {
  network: NetworkInfo; snap: SimSnapshot | null; selected: string | null; onSelect: (id: string) => void;
}) {
  const id = selected ?? network.intersections[0]?.id ?? null;
  const { data, error } = usePoll<Status | null>(() => (id ? api<Status>(`/api/intersections/${id}/status?since_s=300`) : Promise.resolve(null)), 2000, [id]);
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState<"all" | "active" | "busy">("all");
  const info = useMemo(() => Object.fromEntries(network.intersections.map((i) => [i.id, i])), [network]);
  const rows = network.intersections
    .map((i) => {
      const s = snap?.signals[i.id];
      return { i, s, queued: s ? Object.values(s.queues).reduce((a, b) => a + b, 0) : 0 };
    })
    .filter(({ i }) => !q || `${i.name} ${i.id}`.toLowerCase().includes(q.toLowerCase()))
    .filter(({ s, queued }) => filter === "all" || (filter === "active" ? s && s.mode !== "NORMAL" : queued >= 8))
    .sort((a, b) => (filter === "all" && !q ? 0 : b.queued - a.queued));

  return (
    <div className="two-col">
      <div className="card list">
        <div className="card-title">Signalised intersections ({network.intersections.length})</div>
        <div className="row gap" style={{ marginBottom: 6 }}>
          <input placeholder="Search junction or road…" value={q} onChange={(e) => setQ(e.target.value)} style={{ flex: 1, minWidth: 0 }} />
          <select style={{ width: 92 }} value={filter} onChange={(e) => setFilter(e.target.value as typeof filter)}>
            <option value="all">all</option>
            <option value="active">priority / recovery</option>
            <option value="busy">busiest</option>
          </select>
        </div>
        {rows.slice(0, 250).map(({ i, s, queued }) => (
          <div key={i.id} className={`list-item ${i.id === id ? "active" : ""}`} onClick={() => onSelect(i.id)}>
            <div>
              <b>{i.name}</b>{i.signal_source === "inferred" && <span className="muted small" title="no signal tagged in OpenStreetMap; added at a main-road crossing"> · inferred</span>}
            </div>
            <div className="row gap small">
              {s && <span className={`mode mode-${s.mode.toLowerCase()}`}>{s.mode}</span>}
              <span className="muted">{queued.toFixed(0)} queued</span>
            </div>
          </div>
        ))}
        {rows.length > 250 && <div className="muted small">…{rows.length - 250} more – use the search box.</div>}
        {rows.length === 0 && <div className="muted small">No junction matches.</div>}
      </div>
      <div className="col">
        {error && <div className="error">{error}</div>}
        {data && (
          <>
            <div className="card">
              <div className="card-title">
                {data.name}
                <span className={`mode mode-${data.mode.toLowerCase()}`} style={{ marginLeft: 8 }}>{data.mode}</span>
              </div>
              <div className="muted small" style={{ marginTop: -6, marginBottom: 8 }}>
                {data.id} · simulated signal{data.signal_source === "osm" ? " at a junction tagged with traffic signals in OpenStreetMap"
                  : data.signal_source === "inferred" ? " (inferred: two main roads cross here, no signal tagged in OpenStreetMap)" : ""}
              </div>
              <div className="phase-grid">
                {data.groups.map((g, gi) => (
                  <div key={gi} className={`phase ${gi === data.current_group ? "current" : ""}`}>
                    <div className="small muted">Phase group {gi + 1}{gi === data.current_group ? ` · ${data.stage} ${data.elapsed}s / ${data.current_green}s` : ""}</div>
                    {g.map((a) => (
                      <div key={a} className="approach">
                        <span className={`lamp lamp-${data.approaches[a]}`} />
                        <span className="grow">{approachFrom(info[data.id], a) || a} <span className="muted small">{data.approach_names[a]}</span>
                          {data.camera_approaches?.includes(a) && <span className="cam-badge" style={{ marginLeft: 6 }}>📷 live camera</span>}</span>
                        <b>{(data.queues[a] ?? 0).toFixed(0)}</b>
                        <span className="muted small">queued</span>
                      </div>
                    ))}
                  </div>
                ))}
              </div>
              <div className="muted small">
                Queue source: {data.queue_source === "cv" ? "computer vision (where cameras are active)" : "simulation ground truth"} ·
                conflicting-green violations: {data.violations}
              </div>
            </div>

            <SignalActions id={data.id} info={info[data.id]} data={data} />

            <div className="card">
              <div className="card-title">Queue length per approach (last 5 min)</div>
              <ResponsiveContainer width="100%" height={240}>
                <LineChart data={data.queue_history}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--grid)" />
                  <XAxis dataKey="t" tickFormatter={(t) => fmtTime(t)} stroke="var(--muted)" fontSize={11} />
                  <YAxis allowDecimals={false} stroke="var(--muted)" fontSize={11} />
                  <Tooltip labelFormatter={(t) => `t = ${fmtTime(Number(t))}`} contentStyle={{ background: "var(--panel)", border: "1px solid var(--border)" }} />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                  {data.priority_sessions.map((p) => (
                    <ReferenceLine key={p.id} x={Math.round(p.requested_at)} stroke="#8e4ec6" strokeDasharray="4 2" />
                  ))}
                  {Object.keys(data.approaches).map((a, i) => (
                    <Line key={a} dataKey={a} name={approachFrom(info[data.id], a) || a} dot={false} stroke={SERIES[i % SERIES.length]} strokeWidth={2} isAnimationActive={false} />
                  ))}
                </LineChart>
              </ResponsiveContainer>
              <div className="muted small">Purple dashed lines mark emergency priority requests.</div>
            </div>

            {data.active_recovery?.timeline && (
              <div className="card accent-orange">
                <div className="card-title">Active recovery – backlog ΔQ relative to pre-priority baseline</div>
                <ResponsiveContainer width="100%" height={200}>
                  <LineChart data={data.active_recovery.timeline}>
                    <CartesianGrid strokeDasharray="3 3" stroke="var(--grid)" />
                    <XAxis dataKey="t" tickFormatter={(t) => fmtTime(t)} stroke="var(--muted)" fontSize={11} />
                    <YAxis stroke="var(--muted)" fontSize={11} />
                    <ReferenceLine y={0} stroke="var(--muted)" />
                    <Tooltip contentStyle={{ background: "var(--panel)", border: "1px solid var(--border)" }} />
                    {Object.keys(data.approaches).map((a, i) => (
                      <Line key={a} dataKey={a} dot={false} stroke={SERIES[i % SERIES.length]} isAnimationActive={false} />
                    ))}
                  </LineChart>
                </ResponsiveContainer>
                <div className="small muted">
                  Green timings applied: {data.active_recovery.timing_adjustments.map((t) => `G${t.group + 1}=${t.green_s}s`).join(", ") || "none yet"}
                </div>
              </div>
            )}

            <div className="card">
              <div className="card-title">Priority sessions</div>
              {data.priority_sessions.length === 0 ? (
                <div className="muted">No emergency priority at this junction yet.</div>
              ) : (
                <table className="tbl">
                  <thead><tr><th>Session</th><th>Trigger</th><th>Status</th><th>Requested</th><th>ETA</th><th>Ahead</th><th>Hold</th></tr></thead>
                  <tbody>
                    {data.priority_sessions.slice().reverse().map((p) => (
                      <tr key={p.id}>
                        <td>{p.id}</td><td>{p.source === "camera" ? "📷 camera" : p.source === "manual" ? "operator" : p.journey_id}</td><td>{p.status}</td><td>{fmtTime(p.requested_at)}</td>
                        <td>{p.eta_s}s</td><td>{p.queue_ahead}</td><td>{p.hold_time_s ?? "–"}s</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
            <div className="card">
              <div className="card-title">Recent recoveries</div>
              {data.recent_recoveries.length === 0 ? (
                <div className="muted">None yet.</div>
              ) : (
                <table className="tbl">
                  <thead><tr><th>Id</th><th>Status</th><th>Duration</th><th>Peak backlog</th><th>Residual</th></tr></thead>
                  <tbody>
                    {data.recent_recoveries.slice().reverse().map((r) => (
                      <tr key={r.id}>
                        <td>{r.id}</td><td>{r.status}</td><td>{fmtTime(r.duration_s)}</td>
                        <td>{Math.max(0, ...Object.values(r.peak_backlog)).toFixed(1)}</td>
                        <td>{Math.max(0, ...Object.values(r.residual)).toFixed(1)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
