import { useEffect, useState } from "react";
import { API_BASE, NetworkInfo, SimSnapshot, api, post } from "../api";
import { Stat } from "../components/Panels";
import { usePoll } from "../hooks/useSimulation";

interface ApproachEstimate {
  edge: string; name: string; t: number; detected_vehicles: number; estimated_queue: number; queue_used: number;
  queue_m: number; confidence: number; true_vehicles_in_view: number; true_queue_in_view: number;
  sim_queue_whole_approach: number; view_m: number;
}
interface EvalResult {
  frames: number; precision: number; recall: number; f1: number; count_mae: number; queue_mae_in_view: number;
  queue_bias_in_view: number; queue_mae_whole_approach: number; mean_confidence: number; note: string;
}

export default function CVPage({ network, snap }: { network: NetworkInfo; snap: SimSnapshot | null }) {
  // start on the busiest intersection so there is something to look at
  const [node, setNode] = useState(() => {
    const busiest = Object.values(snap?.signals ?? {}).sort(
      (a, b) => Object.values(b.queues).reduce((x, y) => x + y, 0) - Object.values(a.queues).reduce((x, y) => x + y, 0),
    )[0];
    return busiest?.node ?? network.intersections[0]?.id ?? "";
  });
  const approaches = network.intersections.find((i) => i.id === node)?.approaches ?? {};
  const [approach, setApproach] = useState<string>(() => {
    const q = snap?.signals[node]?.queues ?? {};
    return Object.keys(approaches).sort((a, b) => (q[b] ?? 0) - (q[a] ?? 0))[0] ?? "";
  });
  const [tick, setTick] = useState(0);
  const [truth, setTruth] = useState(true);
  const [evalRes, setEvalRes] = useState<EvalResult | null>(null);
  const [evaluating, setEvaluating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { data: est } = usePoll(() => api<{ approaches: ApproachEstimate[]; queue_source: string }>(`/api/cv/${node}/estimate`), 2000, [node]);

  useEffect(() => {
    if (!(approach in approaches)) setApproach(Object.keys(approaches)[0] ?? "");
  }, [node]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const id = window.setInterval(() => setTick((t) => t + 1), 1500);
    return () => window.clearInterval(id);
  }, []);

  const runEval = async () => {
    setEvaluating(true);
    setError(null);
    try {
      setEvalRes(await post<EvalResult>("/api/cv/evaluate?seconds=300"));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setEvaluating(false);
    }
  };

  return (
    <div className="col">
      <div className="card">
        <div className="card-title">Virtual traffic cameras</div>
        <p className="muted small">
          Each approach has an overhead camera covering the last 150 m before the stop line, rendered from the simulation.
          OpenCV background subtraction detects vehicles; comparing consecutive frames separates stopped from moving
          vehicles. Green boxes: moving · red boxes: stopped · thin blue outlines: ground truth.
          {snap?.queue_source === "cv" ? " The controller is currently using these estimates." : " The controller is using simulator queues (toggle on the Live page)."}
        </p>
        <div className="row gap wrap">
          <label className="field-inline">Intersection
            <select value={node} onChange={(e) => setNode(e.target.value)}>
              {network.intersections.map((i) => <option key={i.id} value={i.id}>{i.id}</option>)}
            </select>
          </label>
          <label className="field-inline">Approach
            <select value={approach} onChange={(e) => setApproach(e.target.value)}>
              {Object.keys(approaches).map((a) => <option key={a}>{a}</option>)}
            </select>
          </label>
          <label className="toggle"><input type="checkbox" checked={truth} onChange={(e) => setTruth(e.target.checked)} /> show ground truth</label>
        </div>
        {approach && (
          <img className="cv-frame" alt="camera frame"
            src={`${API_BASE}/api/cv/${node}/frame.png?approach=${encodeURIComponent(approach)}&truth=${truth}&k=${tick}`} />
        )}
      </div>
      <div className="card">
        <div className="card-title">Estimates vs simulation ground truth at {node}</div>
        <table className="tbl">
          <thead><tr><th>Approach</th><th>Detected</th><th>True in view</th><th>Queue est.</th><th>True queue (view)</th><th>True queue (whole road)</th><th>Queue m</th><th>Confidence</th><th>Value used</th></tr></thead>
          <tbody>
            {est?.approaches.map((a) => (
              <tr key={a.edge}>
                <td>{a.edge}<div className="muted small">{a.name}</div></td>
                <td>{a.detected_vehicles}</td><td>{a.true_vehicles_in_view}</td>
                <td><b>{a.estimated_queue}</b></td><td>{a.true_queue_in_view}</td><td>{a.sim_queue_whole_approach}</td>
                <td>{a.queue_m}</td><td>{a.confidence}</td><td>{a.queue_used}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="muted small">
          “Value used” is conservative when confidence is low (no previous frame or merged blobs): the controller never drops a queue estimate sharply on one bad frame.
          Queues longer than the camera's view are under-counted – compare the last two “true queue” columns.
        </div>
      </div>
      <div className="card">
        <div className="row between">
          <div className="card-title">Accuracy evaluation</div>
          <button className="btn" disabled={evaluating} onClick={runEval}>{evaluating ? "Evaluating (≈1 min)…" : "Evaluate 300 s of traffic"}</button>
        </div>
        {error && <div className="error">{error}</div>}
        {evalRes && (
          <>
            <div className="stats">
              <Stat label="Precision" value={evalRes.precision} sub="IoU ≥ 0.5" />
              <Stat label="Recall" value={evalRes.recall} sub={`F1 ${evalRes.f1}`} />
              <Stat label="Count MAE" value={evalRes.count_mae} sub="vehicles / frame" />
              <Stat label="Queue MAE" value={evalRes.queue_mae_in_view} sub={`bias ${evalRes.queue_bias_in_view} · whole road ${evalRes.queue_mae_whole_approach}`} />
            </div>
            <div className="muted small">{evalRes.frames} frames scored. {evalRes.note}</div>
          </>
        )}
      </div>
    </div>
  );
}
