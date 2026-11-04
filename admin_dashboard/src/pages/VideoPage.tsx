import { useEffect, useRef, useState, type RefObject } from "react";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  API_BASE, DEFAULT_LINE, DEFAULT_ROI, NetworkInfo, VideoCaps, VideoSourceInfo, VideoStats, ZoneCfg, api,
  fmtTime, localStorageGet, post, wsUrl,
} from "../api";
import { Stat } from "../components/Panels";
import { usePoll } from "../hooks/useSimulation";
import { approachFrom } from "./IntersectionsPage";

const CONGESTION_COLOR: Record<string, string> = { free: "#30a46c", moderate: "#f5d90a", heavy: "#f76b15", jam: "#e5484d" };

type BoxTrack = { id: number; cls: string; conf: number; stopped: boolean; box: number[]; ambulance?: boolean; amb_score?: number; reasons?: string[]; road?: boolean };
type SelfTest = { ok: boolean; detector: string; ms?: number; detections?: { cls: string; conf: number }[]; error?: string; hint?: string; info?: { weights?: string } };
const DETECT_LABEL = { all: "all objects (check the camera works)", road: "vehicles + people", vehicles: "road vehicles only" } as const;

/** Runs the detector on a bundled photo so you can see at once whether object detection works. */
function ModelCheck({ detector, auto }: { detector: string; auto: boolean }) {
  const [res, setRes] = useState<SelfTest | null>(null);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true);
    try { setRes(await api<SelfTest>(`/api/video/selftest?detector=${detector}`)); }
    catch (e) { setRes({ ok: false, detector, error: (e as Error).message }); }
    finally { setBusy(false); }
  };
  useEffect(() => { if (auto) run(); }, [detector, auto]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div className={`model-check ${res ? (res.ok ? "ok" : "bad") : ""}`}>
      <button className="btn ghost small-btn" disabled={busy} onClick={run}>{busy ? "Testing…" : "Test detector"}</button>{" "}
      {res && res.ok && <span className="small">✓ {res.detector}{res.info?.weights ? ` (${res.info.weights})` : ""} works – found {res.detections!.map((d) => d.cls).join(", ")} in the test photo in {res.ms} ms.</span>}
      {res && !res.ok && <span className="small">✗ {res.error} {res.hint ?? ""}</span>}
      {!res && <span className="muted small">Checks that the object-detection model loads and finds objects in a test photo.</span>}
    </div>
  );
}

async function sendJson<T>(path: string, method: string, body?: unknown): Promise<T> {
  return api<T>(path, { method, body: body === undefined ? undefined : JSON.stringify(body) });
}

/** Live updates for one source over /ws/video/{id}, reconnecting automatically. */
function useVideoLive(sid: string | null) {
  const [info, setInfo] = useState<VideoSourceInfo | null>(null);
  const [live, setLive] = useState(false);
  useEffect(() => {
    if (!sid) return;
    setInfo(null);
    let ws: WebSocket | null = null;
    let closed = false;
    let retry: number | undefined;
    const connect = () => {
      ws = new WebSocket(wsUrl(`/ws/video/${sid}`));
      ws.onmessage = (m) => {
        setInfo(JSON.parse(m.data));
        setLive(true);
      };
      ws.onclose = () => {
        setLive(false);
        if (!closed) retry = window.setTimeout(connect, 2000);
      };
    };
    connect();
    const ping = window.setInterval(() => ws?.readyState === WebSocket.OPEN && ws.send("ping"), 10000);
    return () => {
      closed = true;
      window.clearTimeout(retry);
      window.clearInterval(ping);
      ws?.close();
    };
  }, [sid]);
  return { info, live };
}

// ------------------------------------------------------------------- overlay
type EditMode = "none" | "roi" | "line";

function Overlay({ target, zone, tracks, edit, points, onClick }: {
  target: RefObject<HTMLElement>; zone?: ZoneCfg; tracks?: BoxTrack[]; edit: EditMode; points: number[][];
  onClick?: (p: number[]) => void;
}) {
  const ref = useRef<HTMLCanvasElement>(null);
  const [size, setSize] = useState<[number, number]>([0, 0]);
  useEffect(() => {
    const el = target.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize([el.clientWidth, el.clientHeight]));
    ro.observe(el);
    return () => ro.disconnect();
  }, [target]);
  useEffect(() => {
    const c = ref.current;
    if (!c) return;
    const [w, h] = size;
    c.width = w;
    c.height = h;
    const g = c.getContext("2d")!;
    g.clearRect(0, 0, w, h);
    const poly = (pts: number[][], close: boolean, color: string, fill?: string) => {
      if (pts.length === 0) return;
      g.beginPath();
      pts.forEach(([x, y], i) => (i ? g.lineTo(x * w, y * h) : g.moveTo(x * w, y * h)));
      if (close) g.closePath();
      if (fill) {
        g.fillStyle = fill;
        g.fill();
      }
      g.strokeStyle = color;
      g.lineWidth = 2;
      g.stroke();
      pts.forEach(([x, y]) => {
        g.fillStyle = color;
        g.fillRect(x * w - 3, y * h - 3, 6, 6);
      });
    };
    if (tracks) {
      poly(zone?.roi ?? DEFAULT_ROI, true, "rgba(255,160,40,0.9)", "rgba(255,160,40,0.08)");
      poly(zone?.line ?? DEFAULT_LINE, false, "#ff3b3b");
      for (const t of tracks) {
        const [x1, y1, x2, y2] = t.box;
        if (t.road === false) { // other objects (all-objects mode): thin grey box
          g.strokeStyle = "rgba(220,220,220,0.9)";
          g.lineWidth = 1.5;
          g.strokeRect(x1 * w, y1 * h, (x2 - x1) * w, (y2 - y1) * h);
          const lb = `${t.cls} ${t.conf.toFixed(2)}`;
          g.font = "12px sans-serif";
          g.fillStyle = "rgba(220,220,220,0.9)";
          g.fillRect(x1 * w, y1 * h - 15, g.measureText(lb).width + 8, 15);
          g.fillStyle = "#111";
          g.fillText(lb, x1 * w + 4, y1 * h - 4);
          continue;
        }
        const col = t.ambulance ? "#ff2d55" : t.stopped ? "#ff9f0a" : "#3ee07a";
        g.strokeStyle = col;
        g.lineWidth = t.ambulance ? 5 : t.stopped ? 3 : 2;
        g.strokeRect(x1 * w, y1 * h, (x2 - x1) * w, (y2 - y1) * h);
        const label = t.ambulance ? `#${t.id} AMBULANCE ${(t.amb_score ?? 0).toFixed(2)}` : `#${t.id} ${t.cls} ${t.conf.toFixed(2)}`;
        g.font = t.ambulance ? "bold 14px sans-serif" : "12px sans-serif";
        const tw = g.measureText(label).width;
        g.fillStyle = col;
        g.fillRect(x1 * w, y1 * h - 16, tw + 8, 16);
        g.fillStyle = "#111";
        g.fillText(label, x1 * w + 4, y1 * h - 4);
      }
    }
    if (edit !== "none") poly(points, edit === "roi" && points.length > 2, edit === "roi" ? "#ffd60a" : "#ff3b3b", edit === "roi" ? "rgba(255,214,10,0.15)" : undefined);
  }, [size, zone, tracks, edit, points]);
  return (
    <canvas
      ref={ref}
      className={`overlay ${edit !== "none" ? "editing" : ""}`}
      onClick={(e) => {
        if (edit === "none" || !onClick) return;
        const r = (e.target as HTMLCanvasElement).getBoundingClientRect();
        onClick([Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))]);
      }}
    />
  );
}

// ------------------------------------------------------------ browser camera
const MAX_IN_FLIGHT = 2; // frames on their way / being processed: keeps the server busy without queueing up lag

function BrowserCamera({ source, onResult, zone, edit, points, onPoint, onSnapshot }: {
  source: VideoSourceInfo; onResult: (s: VideoStats, fps: number) => void; zone: ZoneCfg; edit: EditMode;
  points: number[][]; onPoint: (p: number[]) => void; onSnapshot?: (b: Blob) => void;
}) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const boxRef = useRef<HTMLDivElement>(null);
  const [tracks, setTracks] = useState<BoxTrack[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [link, setLink] = useState<{ state: "idle" | "connecting" | "live" | "reconnecting"; attempt: number; fps: number }>({ state: "idle", attempt: 0, fps: 0 });
  useEffect(() => {
    if (!running) {
      setLink({ state: "idle", attempt: 0, fps: 0 });
      return;
    }
    let stream: MediaStream | null = null;
    let ws: WebSocket | null = null;
    let stopped = false;
    let attempt = 0;
    let inFlight = 0;
    let lastReply = performance.now();
    let retry: number | undefined;
    const replies: number[] = [];
    const canvas = document.createElement("canvas");
    const params = new URLSearchParams({ name: source.name, detector: source.detector, detect: source.detect ?? "all",
      conf: String(source.conf), imgsz: String(source.imgsz ?? 480) });

    const sendFrame = () => {
      const v = videoRef.current;
      if (stopped || !ws || ws.readyState !== WebSocket.OPEN || inFlight >= MAX_IN_FLIGHT) return;
      if (!v || !v.videoWidth || v.readyState < 2) {
        window.setTimeout(sendFrame, 100);
        return;
      }
      const scale = Math.min(1, 640 / v.videoWidth);
      canvas.width = Math.round(v.videoWidth * scale);
      canvas.height = Math.round(v.videoHeight * scale);
      canvas.getContext("2d")!.drawImage(v, 0, 0, canvas.width, canvas.height);
      inFlight += 1;
      canvas.toBlob((b) => {
        if (b && ws?.readyState === WebSocket.OPEN) {
          ws.send(b);
          if (inFlight < MAX_IN_FLIGHT) window.setTimeout(sendFrame, 15); // fill the pipeline
        } else inFlight = Math.max(0, inFlight - 1);
      }, "image/jpeg", 0.72);
    };

    const connect = () => {
      if (stopped) return;
      setLink((l) => ({ ...l, state: attempt ? "reconnecting" : "connecting", attempt }));
      ws = new WebSocket(wsUrl(`/ws/video/${source.id}/push?${params}`));
      ws.binaryType = "arraybuffer";
      inFlight = 0;
      ws.onopen = () => {
        attempt = 0;
        lastReply = performance.now();
        setError(null);
        setLink((l) => ({ ...l, state: "live", attempt: 0 }));
        sendFrame();
      };
      ws.onmessage = (m) => {
        const r = JSON.parse(m.data);
        if (r.pong) return;
        inFlight = Math.max(0, inFlight - 1);
        const now = performance.now();
        lastReply = now;
        replies.push(now);
        while (replies.length && now - replies[0] > 2000) replies.shift();
        if (r.tracks) {
          setTracks(r.tracks);
          onResult(r.stats, r.proc_fps);
        }
        setLink((l) => ({ ...l, fps: replies.length / 2 }));
        sendFrame();
      };
      ws.onclose = (e) => {
        if (stopped) return;
        if (e.code === 4404) {
          setError("This camera source no longer exists on the server – remove it and add the browser camera again.");
          setRunning(false);
          return;
        }
        attempt += 1; // the backend restarted, Wi-Fi blinked, the laptop slept …: reconnect, camera stays on
        setLink((l) => ({ ...l, state: "reconnecting", attempt }));
        retry = window.setTimeout(connect, Math.min(5000, 300 * 2 ** Math.min(attempt, 4)));
      };
      ws.onerror = () => ws?.close();
    };

    // watchdog: a lost reply must not stall the loop; a dead socket is replaced
    const watchdog = window.setInterval(() => {
      if (!ws || ws.readyState !== WebSocket.OPEN) return;
      const idle = performance.now() - lastReply;
      if (idle > 4000 && inFlight > 0) {
        inFlight = 0;
        sendFrame();
      }
      if (idle > 12000) ws.close();
      else if (idle > 3000) ws.send("ping");
    }, 1000);

    (async () => {
      try {
        if (!navigator.mediaDevices?.getUserMedia) {
          throw new Error("Camera access needs HTTPS or http://localhost. On a phone, use the IP Webcam app + 'Camera stream' instead.");
        }
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment", width: { ideal: 1280 }, frameRate: { ideal: 30 } }, audio: false });
        stream.getVideoTracks()[0]?.addEventListener("ended", () => !stopped && setError("The camera was turned off or unplugged – press Start camera again."));
        if (videoRef.current) {
          videoRef.current.srcObject = stream;
          await videoRef.current.play();
        }
        connect();
      } catch (e) {
        setError((e as Error).message);
        setRunning(false);
      }
    })();
    return () => {
      stopped = true;
      window.clearTimeout(retry);
      window.clearInterval(watchdog);
      ws?.close();
      stream?.getTracks().forEach((t) => t.stop());
    };
  }, [running, source.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const snapshot = () => {
    const v = videoRef.current;
    if (!v || !v.videoWidth || !onSnapshot) return;
    const c = document.createElement("canvas");
    c.width = v.videoWidth;
    c.height = v.videoHeight;
    c.getContext("2d")!.drawImage(v, 0, 0);
    c.toBlob((b) => b && onSnapshot(b), "image/jpeg", 0.92);
  };

  return (
    <div>
      <div className="row gap wrap" style={{ marginBottom: 8 }}>
        <button className="btn" onClick={() => { setError(null); setRunning(!running); }}>{running ? "Stop camera" : "Start camera"}</button>
        {running && onSnapshot && <button className="btn ghost" onClick={snapshot}>📸 Analyse a photo of this view</button>}
        {running && (
          <span className={`link link-${link.state === "live" ? "live" : "stale"}`}>
            {link.state === "live" ? `connected · ${link.fps.toFixed(1)} fps` : link.state === "connecting" ? "connecting…" : `reconnecting (attempt ${link.attempt})… camera stays on`}
          </span>
        )}
        <span className="muted small">Frames are sent to the backend, detected with {source.detector_info?.name ?? source.detector} and drawn here.</span>
      </div>
      {error && <div className="error">{error}</div>}
      <div className="video-box" ref={boxRef}>
        <video ref={videoRef} muted playsInline className="video-el" />
        <Overlay target={boxRef as RefObject<HTMLElement>} zone={zone} tracks={tracks} edit={edit} points={points} onClick={onPoint} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- photos
export interface PhotoItem {
  name: string; error?: string; annotated?: string; original?: string; width?: number; height?: number; ms?: number;
  vehicles?: number; people?: number; other_objects?: number; vehicles_by_class?: Record<string, number>;
  objects?: Record<string, number>; occupancy?: number; congestion?: string; ambulances?: number;
  ambulance_details?: { score: number; reasons: string[] }[];
}
export interface PhotoBatch {
  id: string; name: string; created: number; detector: string; weights?: string | null; photos: number; processing_s: number;
  settings: { conf: number; detect: string; ambulance: boolean; imgsz: number; quality?: string; text_check?: boolean; classifier?: boolean };
  summary: { vehicles: number; people: number; vehicles_by_class: Record<string, number>; ambulances: number;
    photos_with_ambulance: number; mean_occupancy: number; congestion: Record<string, number>; mean_ms: number };
  items: PhotoItem[]; files?: Record<string, string>; thumb?: string | null;
}

function uploadPhotos(files: (File | Blob)[], opts: Record<string, string>, onProgress?: (f: number) => void): Promise<PhotoBatch> {
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    files.forEach((f, i) => fd.append("files", f, f instanceof File ? f.name : `camera-snapshot-${Date.now()}-${i}.jpg`));
    Object.entries(opts).forEach(([k, v]) => fd.append(k, v));
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_BASE}/api/video/photos`);
    const token = localStorageGet("adminToken");
    if (token) xhr.setRequestHeader("X-Admin-Token", token);
    xhr.upload.onprogress = (e) => e.lengthComputable && onProgress?.(e.loaded / e.total);
    xhr.onload = () => {
      if (xhr.status < 300) resolve(JSON.parse(xhr.responseText));
      else {
        try { reject(new Error(JSON.parse(xhr.responseText).detail)); } catch { reject(new Error(xhr.statusText)); }
      }
    };
    xhr.onerror = () => reject(new Error("upload failed – is the backend running?"));
    xhr.send(fd);
  });
}

function PhotoView({ bid, onDeleted }: { bid: string; onDeleted: () => void }) {
  const { data: b, error } = usePoll(() => api<PhotoBatch>(`/api/video/photos/${bid}`), 0, [bid]);
  const [idx, setIdx] = useState(0);
  const [showOrig, setShowOrig] = useState(false);
  useEffect(() => setIdx(0), [bid]);
  if (error) return <div className="card error">{error}</div>;
  if (!b) return <div className="card muted">Loading photo analysis…</div>;
  const file = (n?: string | null) => (n ? `${API_BASE}/api/video/photos/${bid}/files/${n}` : "");
  const it = b.items[Math.min(idx, b.items.length - 1)];
  const byClass = Object.entries(b.summary.vehicles_by_class).sort((x, y) => y[1] - x[1]);
  const maxC = Math.max(1, ...byClass.map(([, n]) => n));
  return (
    <div className="col">
      <div className="card">
        <div className="row between wrap gap">
          <div>
            <div className="card-title" style={{ marginBottom: 2 }}>📷 {b.name}</div>
            <div className="muted small">
              {b.photos} photo{b.photos === 1 ? "" : "s"} · {b.detector === "ensemble" ? "accurate: YOLO11m on the whole photo + tiles" : b.detector}{b.settings.classifier ? " · trained ambulance/vehicle classifier" : ""}{b.weights ? ` (${b.weights})` : ""}{b.settings.text_check ? " · text check" : ""} ·
              confidence {b.settings.conf} · {b.summary.mean_ms} ms per photo · {new Date(b.created * 1000).toLocaleString()}
            </div>
          </div>
          <div className="row gap">
            {b.files && Object.entries(b.files).map(([k, v]) => <a key={k} className="btn ghost" href={file(v)} download>{k.toUpperCase()} ↓</a>)}
            <button className="btn danger" onClick={async () => { await sendJson(`/api/video/photos/${bid}`, "DELETE"); onDeleted(); }}>Delete</button>
          </div>
        </div>
      </div>
      <div className="stats">
        <Stat label="Vehicles" value={b.summary.vehicles} sub={byClass.map(([c, n]) => `${n} ${c}`).join(", ") || "none"} />
        <Stat label="People" value={b.summary.people} />
        <Stat label="Ambulances" value={<span className={b.summary.ambulances ? "bad" : ""}>{b.summary.ambulances}</span>} sub={`${b.summary.photos_with_ambulance} photo(s)`} />
        <Stat label="Road covered" value={`${Math.round(b.summary.mean_occupancy * 100)}%`} sub="mean vehicle occupancy" />
        <Stat label="Congestion" value={Object.entries(b.summary.congestion).filter(([, n]) => n).map(([k, n]) => `${n} ${k}`).join(" · ") || "–"} />
      </div>
      {it && (
        <div className="card">
          <div className="row between wrap gap" style={{ marginBottom: 8 }}>
            <b>{it.name}</b>
            {!it.error && (
              <label className="toggle small"><input type="checkbox" checked={showOrig} onChange={(e) => setShowOrig(e.target.checked)} /> show original</label>
            )}
          </div>
          {it.error ? <div className="error">{it.error}</div> : (
            <>
              <a href={file(showOrig ? it.original : it.annotated)} target="_blank" rel="noreferrer">
                <img className="video-el photo-main" alt={it.name} src={file(showOrig ? it.original : it.annotated)} />
              </a>
              <div className="row gap wrap small" style={{ marginTop: 8 }}>
                <span><b>{it.vehicles}</b> vehicles{it.vehicles_by_class && Object.keys(it.vehicles_by_class).length ? ` (${Object.entries(it.vehicles_by_class).map(([c, n]) => `${n} ${c}`).join(", ")})` : ""}</span>
                <span>· <b>{it.people}</b> people</span>
                <span>· occupancy <b>{Math.round((it.occupancy ?? 0) * 100)}%</b></span>
                <span>· congestion <b style={{ color: CONGESTION_COLOR[it.congestion ?? "free"] }}>{it.congestion}</b></span>
                <span className="muted">· {it.width}×{it.height} · {it.ms} ms</span>
              </div>
              {it.objects && <div className="muted small" style={{ marginTop: 4 }}>All objects: {Object.entries(it.objects).map(([c, n]) => `${n} ${c}`).join(", ") || "none"}</div>}
              {(it.ambulances ?? 0) > 0 && (
                <div className="amb-alert"><b>🚑 Ambulance in this photo</b> {it.ambulance_details?.map((a, i) => <span key={i} className="small"> score {a.score} ({a.reasons.join(", ")})</span>)}
                  <div className="small muted">A still photo cannot show a flashing beacon – the check uses the model, a lit red/blue light bar and the livery, so treat it as less certain than the video check.</div>
                </div>
              )}
            </>
          )}
        </div>
      )}
      {b.items.length > 1 && (
        <div className="card">
          <div className="card-title">All photos</div>
          <div className="photo-grid">
            {b.items.map((x, i) => (
              <div key={i} className={`photo-thumb ${i === idx ? "active" : ""}`} onClick={() => setIdx(i)}>
                {x.annotated ? <img src={file(x.annotated)} alt={x.name} loading="lazy" /> : <div className="error small">{x.error}</div>}
                <div className="small"><b>{x.vehicles ?? "–"}</b> veh{x.ambulances ? " · 🚑" : ""} · <span style={{ color: CONGESTION_COLOR[x.congestion ?? "free"] }}>{x.congestion ?? ""}</span></div>
              </div>
            ))}
          </div>
          {byClass.length > 0 && (
            <div style={{ marginTop: 10 }}>
              {byClass.map(([c, n]) => (
                <div key={c} className="hbar"><span>{c}</span><div><i style={{ width: `${(100 * n) / maxC}%` }} /></div><b>{n}</b></div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- add source
function AddSource({ caps, network, onCreated }: { caps: VideoCaps | null; network: NetworkInfo; onCreated: (id: string) => void }) {
  const [kind, setKind] = useState<"upload" | "photo" | "sample" | "url" | "webcam" | "push">("upload");
  const [photos, setPhotos] = useState<File[]>([]);
  const [quality, setQuality] = useState<"accurate" | "fast">("accurate");
  const [name, setName] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [sample, setSample] = useState("");
  const [url, setUrl] = useState("");
  const [cam, setCam] = useState("0");
  const [mode, setMode] = useState<"live" | "batch">("live");
  const [detector, setDetector] = useState("auto");
  const [conf, setConf] = useState(0.35);
  const [fps, setFps] = useState(8);
  const [detect, setDetect] = useState<"all" | "road" | "vehicles">("vehicles");
  const [imgsz, setImgsz] = useState(640);
  useEffect(() => { // cameras: show every object so it is obvious detection works; videos: road vehicles
    const cam = kind === "webcam" || kind === "push";
    setDetect(cam ? "all" : "vehicles");
    setConf(cam ? 0.25 : 0.35);
    setImgsz(kind === "photo" ? 960 : cam || kind === "url" ? 480 : 640);
    if (kind === "photo") { setDetect("all"); setConf(0.3); }
    setFps(cam || kind === "url" ? 15 : 8);
  }, [kind]);
  const [amb, setAmb] = useState(true);
  const [cams, setCams] = useState<{ index: number; backend?: string; width?: number; height?: number; in_use_by?: string }[] | null>(null);
  const [scanning, setScanning] = useState(false);
  const scan = async () => {
    setScanning(true);
    try { setCams(await api("/api/video/webcams")); } catch (e) { setError((e as Error).message); } finally { setScanning(false); }
  };
  useEffect(() => { if (kind === "webcam" && cams === null) scan(); }, [kind]); // eslint-disable-line react-hooks/exhaustive-deps
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (caps?.samples.length && !sample) setSample(caps.samples[0]);
  }, [caps]); // eslint-disable-line react-hooks/exhaustive-deps

  const classes = null;
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      if (kind === "photo") {
        if (!photos.length) throw new Error("Choose one or more photos first");
        const b = await uploadPhotos(photos, { detector, conf: String(conf), detect, ambulance: String(amb), imgsz: String(imgsz), name, quality }, setProgress);
        setPhotos([]);
        onCreated(`photo:${b.id}`);
      } else if (kind === "upload") {
        if (!file) throw new Error("Choose a video file first");
        const fd = new FormData();
        fd.append("file", file);
        fd.append("name", name || file.name);
        fd.append("mode", mode);
        fd.append("detector", detector);
        fd.append("conf", String(conf));
        fd.append("target_fps", String(fps));
        const res = await new Promise<VideoSourceInfo>((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          xhr.open("POST", `${API_BASE}/api/video/upload`);
          const token = localStorageGet("adminToken");
          if (token) xhr.setRequestHeader("X-Admin-Token", token);
          xhr.upload.onprogress = (e) => e.lengthComputable && setProgress(e.loaded / e.total);
          xhr.onload = () => {
            if (xhr.status < 300) resolve(JSON.parse(xhr.responseText));
            else {
              try { reject(new Error(JSON.parse(xhr.responseText).detail)); } catch { reject(new Error(xhr.statusText)); }
            }
          };
          xhr.onerror = () => reject(new Error("upload failed"));
          xhr.send(fd);
        });
        await sendJson(`/api/video/sources/${res.id}`, "PATCH", { classes: classes ?? [], detect, imgsz, ambulance: { enabled: amb } });
        onCreated(res.id);
      } else {
        const body: Record<string, unknown> = { name: name || undefined, detector, conf, target_fps: fps, classes, detect, imgsz, ambulance: { enabled: amb } };
        if (kind === "sample") Object.assign(body, { kind: "file", uri: sample, mode, loop: true });
        if (kind === "url") Object.assign(body, { kind: "url", uri: url.trim() });
        if (kind === "webcam") Object.assign(body, { kind: "webcam", uri: cam });
        if (kind === "push") Object.assign(body, { kind: "push", name: name || "Browser camera" });
        const res = await post<VideoSourceInfo>("/api/video/sources", body);
        onCreated(res.id);
      }
      setFile(null);
      setName("");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
      setProgress(null);
    }
  };

  const tab = (k: typeof kind, label: string) => (
    <button key={k} className={`tab ${kind === k ? "active" : ""}`} onClick={() => setKind(k)}>{label}</button>
  );
  return (
    <div className="card">
      <div className="card-title">Add a video source</div>
      <div className="row wrap" style={{ gap: 4, marginBottom: 10 }}>
        {tab("upload", "Upload video")}
        {tab("photo", "Photo")}
        {tab("sample", "Sample video")}
        {tab("url", "Camera stream")}
        {tab("webcam", "Server webcam")}
        {tab("push", "This browser's camera")}
      </div>
      {kind === "photo" && (
        <div className="field">
          <div className={`drop ${photos.length ? "has" : ""}`}
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => { e.preventDefault(); setPhotos(Array.from(e.dataTransfer.files).filter((f) => f.type.startsWith("image/"))); }}>
            <input type="file" accept="image/*" multiple onChange={(e) => setPhotos(Array.from(e.target.files ?? []))} />
            {photos.length ? `${photos.length} photo${photos.length > 1 ? "s" : ""} selected: ${photos.slice(0, 4).map((f) => f.name).join(", ")}${photos.length > 4 ? "…" : ""}`
              : "Drop photos here or click to choose (jpg, png, webp, bmp, tif – up to 100 at once)"}
          </div>
          <div className="row gap small" style={{ marginTop: 6 }}>
            <button className="btn ghost small-btn" disabled={busy} onClick={async () => {
              setBusy(true); setError(null);
              try { const b = await post<PhotoBatch>("/api/video/photos/sample"); onCreated(`photo:${b.id}`); }
              catch (e) { setError((e as Error).message); } finally { setBusy(false); }
            }}>Try the sample street photo</button>
            <label className="field-inline">Quality
              <select value={quality} onChange={(e) => setQuality(e.target.value as "accurate" | "fast")}>
                <option value="accurate">accurate – crowded scenes (YOLO11m + tiles + classifier + text check, a few s/photo)</option>
                <option value="fast">fast – one model</option>
              </select>
            </label>
            <span className="muted">Each photo: every object, vehicles by class, road occupancy, congestion level and an ambulance check; annotated images + CSV/JSON.</span>
          </div>
        </div>
      )}
      {kind === "upload" && (
        <label className="field">Video file (mp4, avi, mov, mkv, webm)
          <input type="file" accept="video/*" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </label>
      )}
      {kind === "sample" && (
        <label className="field">Bundled sample
          <select value={sample} onChange={(e) => setSample(e.target.value)}>
            {caps?.samples.map((s) => <option key={s}>{s}</option>)}
          </select>
        </label>
      )}
      {kind === "url" && (
        <label className="field">Stream URL – RTSP / HTTP(S) / MJPEG
          <input placeholder="rtsp://user:pass@192.168.1.50:554/stream1   or   http://192.168.1.23:8080/video" value={url} onChange={(e) => setUrl(e.target.value)} />
          <span className="muted small">Phone as a live traffic camera: install an “IP Webcam” app, start the server, paste its …/video URL here (same Wi-Fi).</span>
        </label>
      )}
      {kind === "webcam" && (
        <div className="field">Camera on the machine running the backend
          <div className="row gap wrap">
            <select value={cam} onChange={(e) => setCam(e.target.value)}>
              {(cams ?? []).filter((c) => !c.in_use_by).map((c) => (
                <option key={c.index} value={String(c.index)}>Camera {c.index} · {c.width}×{c.height} · {c.backend}</option>
              ))}
              {cams && cams.filter((c) => !c.in_use_by).length === 0 && <option value={cam}>Camera {cam} (not detected)</option>}
              {cams === null && <option value={cam}>Camera {cam}</option>}
            </select>
            <button className="btn ghost" onClick={scan} disabled={scanning}>{scanning ? "Scanning…" : "Scan cameras"}</button>
          </div>
          {cams && cams.length === 0 && <span className="small" style={{ color: "#ffb38a" }}>No camera found. Close apps using it (Zoom/Teams/Camera), allow camera access for Python/Terminal (macOS: System Settings › Privacy › Camera), or use “This browser's camera”.</span>}
          {cams?.filter((c) => c.in_use_by).map((c) => <span key={c.index} className="muted small">Camera {c.index} is in use by {c.in_use_by}. </span>)}
        </div>
      )}
      {kind === "push" && <p className="muted small">Uses this device's camera through the browser (allowed on http://localhost or HTTPS). Frames are analysed by the backend.</p>}
      <div className="row gap wrap">
        <label className="field-inline">Name <input value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Palm Beach Rd NB" /></label>
        <label className="field-inline">Detector
          <select value={detector} onChange={(e) => setDetector(e.target.value)}>
            <option value="auto">auto (best available)</option>
            <option value="yolo" disabled={!caps?.detectors.yolo}>YOLO11n (fastest, stock COCO)</option>
            <option value="yoloe" disabled={!caps?.detectors.yoloe}>YOLOE open-vocabulary (ambulance, auto-rickshaw…)</option>
            <option value="opencv-dnn" disabled={!caps?.detectors["opencv-dnn"]}>YOLO via OpenCV DNN (ONNX)</option>
            <option value="motion">OpenCV motion (no ML)</option>
          </select>
        </label>
        {(kind === "upload" || kind === "sample") && (
          <label className="field-inline">Mode
            <select value={mode} onChange={(e) => setMode(e.target.value as "live" | "batch")}>
              <option value="live">live playback</option>
              <option value="batch">batch → annotated video + CSV</option>
            </select>
          </label>
        )}
        <label className="field-inline">Confidence {conf.toFixed(2)}
          <input type="range" min={0.1} max={0.8} step={0.05} value={conf} onChange={(e) => setConf(Number(e.target.value))} />
        </label>
        {kind !== "photo" && <label className="field-inline">Process
          <select value={fps} onChange={(e) => setFps(Number(e.target.value))}>
            {[2, 4, 8, 12, 15, 20, 25, 30].map((f) => <option key={f} value={f}>{f} fps</option>)}
          </select>
        </label>}
        <label className="field-inline">Speed
          <select value={imgsz} onChange={(e) => setImgsz(Number(e.target.value))} title="detector input size">
            <option value={320}>fastest (320 px)</option>
            <option value={480}>balanced (480 px)</option>
            <option value={640}>accurate (640 px)</option>
            <option value={960}>small/far vehicles (960 px)</option>
          </select>
        </label>
        <label className="field-inline">Show
          <select value={detect} onChange={(e) => setDetect(e.target.value as typeof detect)}>
            {(Object.keys(DETECT_LABEL) as (keyof typeof DETECT_LABEL)[]).map((k) => <option key={k} value={k}>{DETECT_LABEL[k]}</option>)}
          </select>
        </label>
        <label className="toggle"><input type="checkbox" checked={amb} onChange={(e) => setAmb(e.target.checked)} /> ambulance recognition</label>
      </div>
      <ModelCheck detector={detector} auto={kind === "webcam" || kind === "push"} />
      <button className="btn" style={{ marginTop: 10 }} disabled={busy} onClick={submit}>
        {busy ? (progress !== null ? (progress < 1 ? `Uploading ${Math.round(progress * 100)}%` : "Analysing…") : "Starting…") : kind === "photo" ? "Analyse photos" : "Start analysis"}
      </button>
      {error && <div className="error">{error}</div>}
      <div className="muted small" style={{ marginTop: 8 }}>
        Models: YOLO {caps?.detectors.yolo ? "✓ installed" : "✗ not installed (pip install -r computer_vision/requirements-yolo.txt)"} ·
        OpenCV DNN {caps?.detectors["opencv-dnn"] ? "✓ ONNX model found" : "✗ run computer_vision/tools/export_onnx.py"} · OpenCV motion ✓
        {network.intersections.length > 0 && " · After starting you can attach the camera to a junction approach so it feeds the signal controller."}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- source view
function SourceView({ sid, network, onDeleted, onPhoto }: { sid: string; network: NetworkInfo; onDeleted: () => void; onPhoto?: (bid: string) => void }) {
  const { info, live } = useVideoLive(sid);
  const [pushStats, setPushStats] = useState<{ s: VideoStats; fps: number } | null>(null);
  const [edit, setEdit] = useState<EditMode>("none");
  const [points, setPoints] = useState<number[][]>([]);
  const [node, setNode] = useState("");
  const [approach, setApproach] = useState("");
  const [target, setTarget] = useState<"field" | "sim">("sim");
  const { data: fieldSnap } = usePoll(() => api<{ junctions: { id: string; name: string; approaches: { id: string; name: string }[] }[] }>("/api/field"), 0);
  const fieldJ = fieldSnap?.junctions;
  const [error, setError] = useState<string | null>(null);
  const imgBox = useRef<HTMLDivElement>(null);
  const [imgKey, setImgKey] = useState(() => Date.now());
  // reopen the MJPEG stream when the connection comes back (backend restart, network blip)
  useEffect(() => { if (live) setImgKey(Date.now()); }, [live]);

  useEffect(() => {
    setEdit("none");
    setPoints([]);
    setPushStats(null);
  }, [sid]);
  useEffect(() => {
    if (info?.attach) {
      setTarget(info.attach.field ? "field" : "sim");
      setNode(info.attach.field ?? info.attach.node ?? "");
      setApproach(info.attach.approach);
    }
  }, [info?.attach?.approach]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!info) return <div className="card muted">Connecting to {sid}…</div>;
  const stats = info.kind === "push" && pushStats ? pushStats.s : info.stats;
  const fps = info.kind === "push" && pushStats ? pushStats.fps : info.proc_fps;
  const zone: ZoneCfg = info.zone ?? {};
  const act = async (fn: () => Promise<unknown>) => {
    setError(null);
    try { await fn(); } catch (e) { setError((e as Error).message); }
  };
  const saveZone = () =>
    act(async () => {
      if (edit === "roi" && points.length < 3) throw new Error("Click at least 3 points around the approach lanes");
      if (edit === "line" && points.length !== 2) throw new Error("Click exactly 2 points for the counting / stop line");
      await sendJson(`/api/video/sources/${sid}`, "PATCH", { zone: edit === "roi" ? { roi: points } : { line: points } });
      setEdit("none");
      setPoints([]);
    });
  const onPoint = (p: number[]) => setPoints((prev) => (edit === "line" && prev.length >= 2 ? [p] : [...prev, p]));
  const nodeInfo = network.intersections.find((i) => i.id === node);
  const approaches = nodeInfo?.approaches ?? {};
  const sortedNodes = [...network.intersections].sort((a, b) => a.name.localeCompare(b.name));
  const byClass = Object.entries(info.vehicles_by_class_total ?? {}).sort((a, b) => b[1] - a[1]);
  const maxClass = Math.max(1, ...byClass.map(([, n]) => n));
  const timeline = info.timeline ?? [];
  const finished = ["finished", "stopped", "error"].includes(info.status);

  return (
    <div className="col">
      <div className="card">
        <div className="row between wrap gap">
          <div>
            <div className="card-title" style={{ marginBottom: 2 }}>{info.name}</div>
            <div className="muted small">
              {info.kind}{info.uri ? ` · ${info.uri}` : ""} · {info.detector_info ? `${info.detector_info.name}${info.detector_info.weights ? ` (${info.detector_info.weights})` : ""}` : info.detector}
              {info.mode === "batch" ? " · batch" : ""} · <span className={`link link-${live ? "live" : "stale"}`}>{info.status}</span>
            </div>
          </div>
          <div className="row gap">
            {!finished && info.kind !== "push" && <button className="btn ghost" onClick={() => act(() => post(`/api/video/sources/${sid}/stop`))}>Stop</button>}
            <button className="btn danger" onClick={() => act(async () => { await sendJson(`/api/video/sources/${sid}`, "DELETE"); onDeleted(); })}>Remove</button>
          </div>
        </div>
        {info.error && <div className="error">{info.error}</div>}
        {info.progress !== null && info.mode === "batch" && (
          <div className="progress"><div style={{ width: `${info.progress * 100}%` }} /></div>
        )}
        <div style={{ marginTop: 10 }}>
          {info.kind === "push" ? (
            <BrowserCamera source={info} zone={zone} edit={edit} points={points} onPoint={onPoint}
              onResult={(s, f) => setPushStats({ s, fps: f })}
              onSnapshot={(blob) => act(async () => {
                const b = await uploadPhotos([blob], { detector: info.detector, conf: String(Math.max(0.3, info.conf)), detect: info.detect ?? "all", ambulance: "true", imgsz: "960", name: `Snapshot from ${info.name}`, quality: "accurate" });
                onPhoto?.(b.id);
              })} />
          ) : (
            <div className="video-box" ref={imgBox}>
              <img className="video-el" alt="annotated video"
                onError={() => window.setTimeout(() => setImgKey(Date.now()), 1500)}
                src={finished ? `${API_BASE}/api/video/sources/${sid}/frame.jpg?k=${info.frames_processed}`
                  : `${API_BASE}/api/video/sources/${sid}/stream.mjpg?k=${imgKey}`} />
              <Overlay target={imgBox as RefObject<HTMLElement>} edit={edit} points={points} onClick={onPoint} />
            </div>
          )}
        </div>
        <div className="row gap wrap" style={{ marginTop: 10 }}>
          {edit === "none" ? (
            <>
              <button className="btn ghost" onClick={() => { setEdit("roi"); setPoints([]); }}>Draw zone (approach lanes)</button>
              <button className="btn ghost" onClick={() => { setEdit("line"); setPoints([]); }}>Draw counting / stop line</button>
              <button className="btn ghost" onClick={() => act(() => sendJson(`/api/video/sources/${sid}`, "PATCH", { zone: { roi: DEFAULT_ROI, line: DEFAULT_LINE } }))}>Reset zone</button>
            </>
          ) : (
            <>
              <span className="muted small">{edit === "roi" ? "Click the corners of the approach lanes on the video, then Save." : "Click the two ends of the stop line, then Save."}</span>
              <button className="btn" onClick={saveZone}>Save</button>
              <button className="btn ghost" onClick={() => { setEdit("none"); setPoints([]); }}>Cancel</button>
            </>
          )}
        </div>
        <div className="row gap wrap small" style={{ marginTop: 8 }}>
          <label className="field-inline">Show
            <select value={info.detect ?? "vehicles"} onChange={(e) => act(() => sendJson(`/api/video/sources/${sid}`, "PATCH", { detect: e.target.value }))}>
              {(Object.keys(DETECT_LABEL) as (keyof typeof DETECT_LABEL)[]).map((k) => <option key={k} value={k}>{DETECT_LABEL[k]}</option>)}
            </select>
          </label>
          <label className="field-inline">Confidence
            <select value={info.conf} onChange={(e) => act(() => sendJson(`/api/video/sources/${sid}`, "PATCH", { conf: Number(e.target.value) }))}>
              {Array.from(new Set([0.15, 0.2, 0.25, 0.35, 0.5, info.conf])).sort().map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </label>
          <label className="field-inline">Speed
            <select value={info.imgsz ?? 640} onChange={(e) => act(() => sendJson(`/api/video/sources/${sid}`, "PATCH", { imgsz: Number(e.target.value) }))}>
              {[320, 480, 640, 960].map((v) => <option key={v} value={v}>{v === 320 ? "fastest" : v === 480 ? "balanced" : v === 640 ? "accurate" : "far vehicles"} ({v} px)</option>)}
            </select>
          </label>
          {info.kind !== "push" && info.kind !== "file" && (
            <label className="field-inline">Max
              <select value={info.target_fps} onChange={(e) => act(() => sendJson(`/api/video/sources/${sid}`, "PATCH", { target_fps: Number(e.target.value) }))}>
                {Array.from(new Set([4, 8, 12, 15, 20, 30, info.target_fps])).sort((a, b) => a - b).map((f) => <option key={f} value={f}>{f} fps</option>)}
              </select>
            </label>
          )}
          {(info.reconnects ?? 0) > 0 && <span className="muted">camera reconnected {info.reconnects}×</span>}
          <span className="muted">
            {info.objects_now && Object.keys(info.objects_now).length > 0
              ? `In view now: ${Object.entries(info.objects_now).map(([c, n]) => `${n} ${c}`).join(", ")}`
              : info.frames_processed > 0 ? "Nothing detected in the current frame." : ""}
          </span>
        </div>
        {info.detector_info?.name === "motion" && (
          <div className="warn small">⚠ Running OpenCV motion detection (no object model): it only finds things that <i>move</i> against a still background and cannot tell what they are. Re-run the setup script to install YOLO, or pick “YOLO via OpenCV DNN”.</div>
        )}
        {error && <div className="error">{error}</div>}
      </div>

      <div className="stats">
        <Stat label="Vehicles in zone" value={stats?.vehicles ?? "–"} sub={`${stats?.tracks_total ?? 0} tracked so far`} />
        <Stat label="Queue (stopped)" value={stats?.queue ?? "–"} sub={stats?.queue_length_m ? `≈ ${stats.queue_length_m} m` : `${Math.round(stats?.queue_length_px ?? 0)} px from line`} />
        <Stat label="Flow" value={stats ? `${stats.flow_per_min}/min` : "–"} sub={`${stats?.crossings_total ?? 0} crossed the line`} />
        <Stat label="Congestion" value={<span style={{ color: CONGESTION_COLOR[stats?.congestion ?? "free"] }}>{stats?.congestion ?? "–"}</span>}
          sub={`occupancy ${Math.round((stats?.occupancy ?? 0) * 100)}%`} />
        <Stat label="Speed" value={stats?.mean_speed != null ? `${stats.mean_speed}` : "–"} sub={stats?.speed_unit ?? ""} />
        <Stat label="Processing" value={`${fps?.toFixed?.(1) ?? fps} fps`} sub={info.video_fps ? `video ${info.video_fps.toFixed(0)} fps` : info.frame_size ? `${info.frame_size[0]}×${info.frame_size[1]}` : ""} />
      </div>

      {(info.ambulances_now?.length ?? 0) > 0 || (stats?.ambulances ?? 0) > 0 ? (
        <div className="amb-alert">
          <b>🚑 Ambulance in view</b>{" "}
          {info.ambulances_now?.map((a) => (
            <span key={a.track} className="small"> #{a.track} score {a.score} ({a.reasons.join(", ") || "model"}) {a.passed ? "– passed the stop line" : a.in_zone ? "– in the approach zone" : ""}</span>
          ))}
          {info.attach && <div className="small">This camera drives {info.attach.field ? `signal ${info.attach.field}` : network.intersections.find((i) => i.id === info.attach!.node)?.name ?? info.attach.node}: the approach is turned green while the ambulance is in the zone.</div>}
        </div>
      ) : (
        <div className="muted small" style={{ margin: "4px 0 8px" }}>
          Ambulance recognition: {info.ambulance?.enabled === false ? "off" : `on – ${info.ambulance_model ? "open-vocabulary model + " : ""}flashing-beacon + livery cues`}.
        </div>
      )}

      <div className="chart-grid">
        <div className="card">
          <div className="card-title small-title">Vehicles by class (unique tracks)</div>
          {byClass.length === 0 && <div className="muted small">Nothing detected yet.</div>}
          {byClass.map(([c, n]) => (
            <div key={c} className="hbar">
              <span>{c}</span>
              <div><i style={{ width: `${(100 * n) / maxClass}%` }} /></div>
              <b>{n}</b>
            </div>
          ))}
          {stats && Object.keys(stats.by_class).length > 0 && (
            <div className="muted small" style={{ marginTop: 6 }}>Now in zone: {Object.entries(stats.by_class).map(([c, n]) => `${n} ${c}`).join(", ")}</div>
          )}
        </div>
        <div className="card">
          <div className="card-title small-title">Last 2 minutes</div>
          {info.kind === "push" ? (
            <div className="muted small">Timeline is kept on the server for browser-camera sessions; see the live numbers above.</div>
          ) : (
            <ResponsiveContainer width="100%" height={190}>
              <LineChart data={timeline}>
                <CartesianGrid strokeDasharray="3 3" stroke="var(--grid)" />
                <XAxis dataKey="t" tickFormatter={(t) => fmtTime(t)} stroke="var(--muted)" fontSize={11} />
                <YAxis stroke="var(--muted)" fontSize={11} allowDecimals={false} />
                <Tooltip contentStyle={{ background: "var(--panel)", border: "1px solid var(--border)" }} labelFormatter={(t) => fmtTime(Number(t))} />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                <Line dataKey="vehicles" stroke="#3e63dd" dot={false} isAnimationActive={false} />
                <Line dataKey="queue" stroke="#e5484d" dot={false} isAnimationActive={false} />
                <Line dataKey="flow_per_min" name="flow/min" stroke="#30a46c" dot={false} isAnimationActive={false} />
              </LineChart>
            </ResponsiveContainer>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-title">Connect this camera to a signal</div>
        <p className="muted small">
          <b>Simulated junction</b>: when this camera recognises an ambulance inside its zone, that approach of the simulated
          signal on the map is turned green (safely, via minimum green, amber and all-red). When the ambulance crosses the
          stop line, priority is released and the junction recovers before returning to its normal cycle. The camera's
          measured queue also replaces the simulator's queue for that approach.
          <br /><b>LED / external signal</b>: the same for a stand-alone LED model signal (Signals → advanced).
        </p>
        <div className="row gap wrap">
          <label className="field-inline">Target
            <select value={target} onChange={(e) => { setTarget(e.target.value as "field" | "sim"); setNode(""); setApproach(""); }}>
              <option value="sim">Simulated junction (map)</option>
              <option value="field">LED / external signal (advanced)</option>
            </select>
          </label>
          <label className="field-inline">Junction
            <select value={node} onChange={(e) => { setNode(e.target.value); setApproach(""); }} style={{ maxWidth: 320 }}>
              <option value="">–</option>
              {target === "sim"
                ? sortedNodes.map((i) => <option key={i.id} value={i.id}>{i.name}</option>)
                : (fieldJ ?? []).map((j) => <option key={j.id} value={j.id}>{j.id} · {j.name}</option>)}
            </select>
          </label>
          <label className="field-inline">Approach
            <select value={approach} onChange={(e) => setApproach(e.target.value)} disabled={!node}>
              <option value="">–</option>
              {target === "sim"
                ? Object.keys(approaches).map((a) => <option key={a} value={a}>{approachFrom(nodeInfo, a)} · {nodeInfo?.approach_names?.[a] ?? a}</option>)
                : (fieldJ?.find((j) => j.id === node)?.approaches ?? []).map((a) => <option key={a.id} value={a.id}>{a.id} {a.name}</option>)}
            </select>
          </label>
          <button className="btn" disabled={!node || !approach}
            onClick={() => act(() => sendJson(`/api/video/sources/${sid}`, "PATCH", { attach: target === "sim" ? { node, approach } : { field: node, approach } }))}>Attach</button>
          {info.attach && <button className="btn ghost" onClick={() => act(() => sendJson(`/api/video/sources/${sid}`, "PATCH", { clear_attach: true }))}>Detach</button>}
        </div>
        {info.attach && (
          <div className="good small" style={{ marginTop: 6 }}>
            {info.attach.field ? `Controls approach ${info.attach.approach} of LED / external signal ${info.attach.field} – see Signals → advanced.`
              : `Drives the simulated signal at ${network.intersections.find((i) => i.id === info.attach!.node)?.name ?? info.attach.node} (${approachFrom(network.intersections.find((i) => i.id === info.attach!.node), info.attach.approach)}). See it on the Intersections tab (📷).`}
          </div>
        )}
      </div>

      {(info.events.length > 0 || Object.values(info.outputs ?? {}).some(Boolean)) && (
        <div className="card">
          {info.events.length > 0 && (
            <>
              <div className="card-title">Alerts</div>
              <ul className="timeline">
                {info.events.slice().reverse().map((e, i) => (
                  <li key={i}><span className="ev-t">{fmtTime(e.t)}</span><b className="bad">{e.type.replace(/_/g, " ").toLowerCase()}</b> {e.class ?? ""} #{e.track}{e.conf != null ? ` (${e.conf})` : ""}{e.reasons?.length ? ` – ${e.reasons.join(", ")}` : ""}</li>
                ))}
              </ul>
            </>
          )}
          {Object.values(info.outputs ?? {}).some(Boolean) && (
            <>
              <div className="card-title" style={{ marginTop: 10 }}>Results</div>
              {info.outputs.video && (
                <video className="video-el" controls src={`${API_BASE}/api/video/sources/${sid}/files/${info.outputs.video}`} style={{ maxWidth: 760 }} />
              )}
              <div className="row gap wrap" style={{ marginTop: 8 }}>
                {Object.entries(info.outputs).filter(([, v]) => v).map(([k, v]) => (
                  <a key={k} className="btn ghost" href={`${API_BASE}/api/video/sources/${sid}/files/${v}`} download>{k.replace("_", " ")} ↓</a>
                ))}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------- page
export default function VideoPage({ network }: { network: NetworkInfo }) {
  const { data: caps } = usePoll(() => api<VideoCaps>("/api/video/capabilities"), 0);
  const { data: sources, reload } = usePoll(() => api<VideoSourceInfo[]>("/api/video/sources"), 3000);
  const { data: batches, reload: reloadPhotos } = usePoll(() => api<PhotoBatch[]>("/api/video/photos"), 15000);
  const [sel, setSel] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  useEffect(() => {
    if (!sel && sources?.length) setSel(sources[sources.length - 1].id);
    if (sources && sources.length === 0 && !sel) setAdding(true);
  }, [sources]); // eslint-disable-line react-hooks/exhaustive-deps
  const pick = (id: string) => { setSel(id); setAdding(false); };

  return (
    <div className="two-col">
      <div className="card list">
        <div className="row between">
          <div className="card-title">Video sources</div>
          <button className="btn" onClick={() => setAdding(true)}>+ Add</button>
        </div>
        {sources?.map((s) => (
          <div key={s.id} className={`list-item ${s.id === sel && !adding ? "active" : ""}`} onClick={() => pick(s.id)}>
            <div className="row between">
              <b>{s.name}</b>
              <span className="muted small">{s.status}</span>
            </div>
            <div className="muted small">
              {s.kind}{s.mode === "batch" ? " · batch" : ""} · {s.detector_info?.name ?? s.detector}
              {s.stats ? ` · ${s.stats.vehicles} in zone · queue ${s.stats.queue}` : ""}
              {s.proc_fps ? ` · ${s.proc_fps} fps` : ""}
              {s.attach ? ` · 📷 ${s.attach.approach}` : ""}
            </div>
            {s.progress !== null && s.mode === "batch" && s.status !== "finished" && (
              <div className="progress"><div style={{ width: `${s.progress * 100}%` }} /></div>
            )}
          </div>
        ))}
        {!sources?.length && <div className="muted small">No sources yet.</div>}
        {(batches?.length ?? 0) > 0 && (
          <>
            <div className="card-title" style={{ marginTop: 14 }}>Photo analyses</div>
            {batches!.map((b) => (
              <div key={b.id} className={`list-item ${sel === `photo:${b.id}` && !adding ? "active" : ""}`} onClick={() => pick(`photo:${b.id}`)}>
                <div className="row between"><b>{b.name}</b><span className="muted small">{b.photos} photo{b.photos === 1 ? "" : "s"}</span></div>
                <div className="muted small">{b.summary.vehicles} vehicles · {b.summary.people} people{b.summary.ambulances ? ` · 🚑 ${b.summary.ambulances}` : ""}</div>
              </div>
            ))}
          </>
        )}
      </div>
      <div className="col">
        {adding || !sel ? (
          <AddSource caps={caps} network={network} onCreated={(id) => { pick(id); reload(); reloadPhotos(); }} />
        ) : sel.startsWith("photo:") ? (
          <PhotoView key={sel} bid={sel.slice(6)} onDeleted={() => { setSel(null); reloadPhotos(); }} />
        ) : (
          <SourceView key={sel} sid={sel} network={network} onDeleted={() => { setSel(null); reload(); }}
            onPhoto={(bid) => { reloadPhotos(); pick(`photo:${bid}`); }} />
        )}
      </div>
    </div>
  );
}
