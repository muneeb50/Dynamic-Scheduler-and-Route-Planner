import { useEffect, useState } from "react";
import { Bar, BarChart, CartesianGrid, ErrorBar, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api, post } from "../api";
import { Stat } from "../components/Panels";
import { usePoll } from "../hooks/useSimulation";

interface StatBlock { n: number; mean: number | null; sd: number | null; ci95: number | null }
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Arm = { completed: number; runs: number } & Record<string, any>;
interface DemandBlock {
  baseline: Arm; priority: Arm; paired_difference: Record<string, StatBlock>; drive_time_reduction_pct: number | null;
}
interface ExperimentResult {
  network: string; engine: string; demands: string[]; seeds: number[]; wall_time_s: number;
  scenario: { base_id: string; pickup_node: string; hospital_id: string };
  summary: { by_demand: Record<string, DemandBlock>; safety_violations_total: number };
  files?: string[];
}
interface ExperimentRow { id: number; status: string; progress: number; error: string | null; params: Record<string, unknown>; results?: ExperimentResult }
interface LiveSummary {
  baseline: { journeys: number; mean_travel_time_s: number | null; mean_signal_delay_s: number | null; mean_stops: number | null };
  priority: { journeys: number; mean_travel_time_s: number | null; mean_signal_delay_s: number | null; mean_stops: number | null };
  recoveries: Record<string, { count: number; mean_duration_s: number | null }>;
  safety_violations_live: number;
}

const METRIC_LABELS: [string, string][] = [
  ["drive_time_s", "Ambulance driving time (s)"],
  ["signal_delay_s", "Ambulance delay at signals (s)"],
  ["stops", "Ambulance stops"],
  ["bg_mean_time_loss_s", "Other traffic: mean time loss (s)"],
  ["max_cross_queue", "Max cross-street queue during priority (veh)"],
  ["mean_recovery_s", "Recovery time after passage (s)"],
];

export default function AnalyticsPage() {
  const [demands, setDemands] = useState<string[]>(["low", "medium", "high"]);
  const [seeds, setSeeds] = useState(5);
  const [engine, setEngine] = useState("builtin");
  const [running, setRunning] = useState<ExperimentRow | null>(null);
  const [result, setResult] = useState<ExperimentRow | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { data: list, reload } = usePoll(() => api<ExperimentRow[]>("/api/experiments"), 0);
  const { data: live } = usePoll(() => api<LiveSummary>("/api/analytics/summary"), 5000);

  useEffect(() => {
    const latest = list?.find((e) => e.status === "DONE");
    if (latest && !result) api<ExperimentRow>(`/api/experiments/${latest.id}`).then(setResult);
    const active = list?.find((e) => e.status === "RUNNING" || e.status === "QUEUED");
    if (active && !running) setRunning(active);
  }, [list]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(async () => {
      const e = await api<ExperimentRow>(`/api/experiments/${running.id}`);
      setRunning(e);
      if (e.status === "DONE") {
        setResult(e);
        setRunning(null);
        reload();
      } else if (e.status === "FAILED") {
        setError(e.error);
        setRunning(null);
        reload();
      }
    }, 1500);
    return () => window.clearInterval(id);
  }, [running?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const start = async () => {
    setError(null);
    try {
      const r = await post<{ id: number; status: string }>("/api/experiments", {
        demands, seeds: Array.from({ length: seeds }, (_, i) => i + 1), engine,
      });
      setRunning({ id: r.id, status: r.status, progress: 0, error: null, params: {} });
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const res = result?.results;
  return (
    <div className="col">
      <div className="card">
        <div className="card-title">Baseline vs emergency priority – matched experiment</div>
        <p className="muted small">
          Each demand level and random seed is simulated twice with identical background traffic: once with fixed-time
          signals only, once with route-aware priority and adaptive recovery. Results are averages with 95% confidence
          intervals, not a single run.
        </p>
        <div className="row gap wrap">
          {["low", "medium", "high"].map((d) => (
            <label key={d} className="toggle">
              <input type="checkbox" checked={demands.includes(d)}
                onChange={(e) => setDemands(e.target.checked ? [...demands, d] : demands.filter((x) => x !== d))} />
              {d}
            </label>
          ))}
          <label className="field-inline">Seeds
            <input type="number" min={1} max={20} value={seeds} onChange={(e) => setSeeds(Number(e.target.value))} style={{ width: 60 }} />
          </label>
          <label className="field-inline">Engine
            <select value={engine} onChange={(e) => setEngine(e.target.value)}>
              <option value="builtin">built-in</option>
              <option value="sumo">SUMO</option>
            </select>
          </label>
          <button className="btn" disabled={!!running || demands.length === 0} onClick={start}>
            {running ? `Running… ${Math.round(running.progress * 100)}%` : "Run experiment"}
          </button>
        </div>
        {running && <div className="progress"><div style={{ width: `${running.progress * 100}%` }} /></div>}
        {error && <div className="error">{error}</div>}
      </div>

      {res && (
        <>
          <div className="card">
            <div className="card-title">
              Experiment #{result!.id} · {res.engine} engine · seeds {res.seeds.join(", ")} · {res.wall_time_s}s compute
            </div>
            <div className="stats">
              {Object.entries(res.summary.by_demand).map(([d, b]) => (
                <Stat key={d} label={`${d} demand`} value={`${b.drive_time_reduction_pct ?? "–"}%`}
                  sub={`faster ambulance (${b.baseline.drive_time_s.mean}s → ${b.priority.drive_time_s.mean}s)`} />
              ))}
              <Stat label="Conflicting greens" value={<span className={res.summary.safety_violations_total ? "bad" : "good"}>{res.summary.safety_violations_total}</span>} sub="across all runs" />
            </div>
            <div className="muted small">Scenario: base {res.scenario.base_id} → pickup {res.scenario.pickup_node} → hospital {res.scenario.hospital_id}. {res.files ? `Saved to ${res.files.join(", ")}` : ""}</div>
          </div>
          <div className="chart-grid">
            {METRIC_LABELS.map(([key, label]) => {
              const rows = Object.entries(res.summary.by_demand).map(([d, b]) => ({
                demand: d,
                baseline: b.baseline[key]?.mean ?? null, baselineErr: b.baseline[key]?.ci95 ?? 0,
                priority: b.priority[key]?.mean ?? null, priorityErr: b.priority[key]?.ci95 ?? 0,
              }));
              return (
                <div className="card" key={key}>
                  <div className="card-title small-title">{label}</div>
                  <ResponsiveContainer width="100%" height={200}>
                    <BarChart data={rows}>
                      <CartesianGrid strokeDasharray="3 3" stroke="var(--grid)" />
                      <XAxis dataKey="demand" stroke="var(--muted)" fontSize={11} />
                      <YAxis stroke="var(--muted)" fontSize={11} />
                      <Tooltip contentStyle={{ background: "var(--panel)", border: "1px solid var(--border)" }} />
                      <Legend wrapperStyle={{ fontSize: 11 }} />
                      <Bar dataKey="baseline" fill="#8b8d98" name="baseline">
                        <ErrorBar dataKey="baselineErr" stroke="var(--text)" width={4} />
                      </Bar>
                      <Bar dataKey="priority" fill="#8e4ec6" name="priority + recovery">
                        <ErrorBar dataKey="priorityErr" stroke="var(--text)" width={4} />
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              );
            })}
          </div>
          <div className="card">
            <div className="card-title">Paired differences (priority − baseline, same seed)</div>
            <table className="tbl">
              <thead><tr><th>Demand</th><th>Driving time</th><th>Signal delay</th><th>Stops</th><th>Other traffic time loss</th><th>Completed</th></tr></thead>
              <tbody>
                {Object.entries(res.summary.by_demand).map(([d, b]) => (
                  <tr key={d}>
                    <td>{d}</td>
                    {["drive_time_s", "signal_delay_s", "stops", "bg_mean_time_loss_s"].map((k) => (
                      <td key={k}>{b.paired_difference[k]?.mean ?? "–"} {b.paired_difference[k]?.ci95 != null ? `± ${b.paired_difference[k].ci95}` : ""}</td>
                    ))}
                    <td>{b.baseline.completed}/{b.baseline.runs} · {b.priority.completed}/{b.priority.runs}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="muted small">Negative values mean the priority system reduced the quantity. ± is the 95% confidence half-width.</div>
          </div>
        </>
      )}

      {live && (
        <div className="card">
          <div className="card-title">Live journeys recorded in the database</div>
          <div className="stats">
            <Stat label="Baseline journeys" value={live.baseline.journeys} sub={`mean ${live.baseline.mean_travel_time_s ?? "–"}s · delay ${live.baseline.mean_signal_delay_s ?? "–"}s`} />
            <Stat label="Priority journeys" value={live.priority.journeys} sub={`mean ${live.priority.mean_travel_time_s ?? "–"}s · delay ${live.priority.mean_signal_delay_s ?? "–"}s`} />
            <Stat label="Recoveries" value={Object.values(live.recoveries).reduce((a, r) => a + r.count, 0)}
              sub={Object.entries(live.recoveries).map(([s, r]) => `${s.toLowerCase()} ${r.count}`).join(" · ")} />
          </div>
          <div className="muted small">Live journeys differ in route and traffic, so use the matched experiment above for comparisons.</div>
        </div>
      )}
    </div>
  );
}
