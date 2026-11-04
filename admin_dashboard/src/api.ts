// API client and shared types for the ResQRoute AI backend.

export const API_BASE: string = (import.meta.env.VITE_API_BASE as string | undefined) ?? "";

export function wsUrl(path: string): string {
  const base = API_BASE || window.location.origin;
  return base.replace(/^http/, "ws") + path;
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const token = localStorageGet("adminToken");
  const res = await fetch(API_BASE + path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { "X-Admin-Token": token } : {}),
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* not JSON */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json() as Promise<T>;
}

export const post = <T,>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export function localStorageGet(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

export type LatLon = [number, number];

export interface Hospital { id: string; name: string; lat: number; lon: number; node: string }
export interface Base { id: string; name: string; node: string; lat: number; lon: number }
export interface IntersectionInfo {
  id: string; name: string; lat: number; lon: number; groups: string[][];
  approaches: Record<string, LatLon[]>; signal_source?: string; approach_names?: Record<string, string>;
}
export interface NetworkMeta {
  area_id?: string | null; bbox?: number[]; area_km2?: number; detail?: string; osm_signals?: number;
  inferred_signals?: number; osm_ways?: number; hospitals_from_osm?: boolean;
}
export interface NetworkInfo {
  name: string; source: string; description: string; nodes: number; edges: number; signals: number;
  bbox: [number, number, number, number]; network_version?: number; lane_km?: number; meta?: NetworkMeta;
  roads: GeoJSON.FeatureCollection;
  intersections: IntersectionInfo[];
  hospitals: Hospital[];
  bases: Base[];
}

export interface SignalState {
  node: string; mode: "NORMAL" | "PRIORITY" | "RECOVERY"; stage: "GREEN" | "YELLOW" | "ALL_RED";
  current_group: number; elapsed: number; current_green: number; groups: string[][];
  approaches: Record<string, "G" | "Y" | "R">; queues: Record<string, number>;
  priority_owner: string | null; recovery: string | null; violations: number; camera_approaches?: string[];
}

export interface AmbulanceState {
  id: string; name: string; status: string; journey_id: string | null; lat: number; lon: number;
  speed: number; base_node: string; vehicle_id: string | null; route: LatLon[];
}

export interface PrioritySession {
  id: string; node: string; approach: string; group: number; vehicle_id: string; journey_id: string;
  status: string; requested_at: number; granted_at: number | null; released_at: number | null;
  eta_s: number; distance_m: number; queue_ahead: number; lead_time_s: number; hold_time_s: number | null;
  source?: "route" | "camera" | "manual"; detail?: Record<string, unknown>;
}

export interface RecoveryState {
  id: string; node: string; status: string; started_at: number; completed_at: number | null;
  duration_s: number | null; baseline: Record<string, number>; peak_backlog: Record<string, number>;
  residual: Record<string, number>; timing_adjustments: { group: number; green_s: number; backlog_per_lane: number }[];
  timeline?: Record<string, number>[];
}

export interface JourneyState {
  id: string; status: string; ambulance_id: string | null; pickup: LatLon; pickup_node: string;
  hospital_id: string; eta_s: number | null; ambulance: AmbulanceState | null; route: LatLon[];
  failure_reason: string | null; metrics: JourneyMetrics | null; created_at: number;
}

export interface JourneyMetrics {
  status: string; total_time_s: number | null; to_pickup_s: number | null; to_hospital_s: number | null;
  free_flow_s: number; distance_m: number; stops: number; waiting_s: number; signal_delay_s: number;
  intersections: { node: string; approach: string; delay_s: number; priority: boolean }[];
  priority_requests: number; priority_granted: number; priority_timeouts: number; mean_hold_s: number | null;
  recoveries: number; recoveries_completed: number; recoveries_timeout: number; mean_recovery_s: number | null;
  failure_reason: string | null;
}

export interface SimEvent { type: string; t: number; [k: string]: unknown }

export interface SimSnapshot {
  type: "simulation"; t: number; engine: string; demand: string; priority_enabled: boolean;
  recovery_enabled: boolean; queue_source: string; running: boolean; speed: number; run_id: number;
  signals: Record<string, SignalState>; ambulances: AmbulanceState[]; priority: PrioritySession[];
  recovery: RecoveryState[]; journeys: JourneyState[]; vehicles: [number, number, number, number, number?][];
  vehicle_count: number; events: SimEvent[]; safety_violations: number; cameras?: CameraSummary[];
  network_version?: number; map?: { state: string; message: string }; auto_emergencies_per_hour?: number;
  test_ambulances?: { id: string; node: string; approach: string; lat: number; lon: number; speed: number }[];
}

export interface SimStatus {
  running: boolean; speed: number; run_id: number; sim_time: number; error: string | null;
  params: { demand: string; priority: boolean; recovery: boolean; queue_source: string; seed: number; engine: string };
  network: { name: string; source: string; nodes: number; edges: number; signals: number };
  vehicles: number; teleports: number; safety_violations: number; demand_levels: Record<string, number>;
  demand_scale?: number; vehicles_per_hour?: number; network_version?: number; auto_emergencies_per_hour?: number;
  ambulances?: number; map?: MapStatus;
}

export interface MapStatus { state: string; message: string; area?: { id: string; name: string; bbox: number[] | null; area_km2?: number } | null; info?: Record<string, unknown> }
export interface MapArea { id: string; name: string; bbox: number[] | null; note: string; area_km2?: number; preset: boolean; cached: boolean }
export interface MapAreas { areas: MapArea[]; current: { name: string; source: string; key: string | null; nodes: number; edges: number; signals: number; lane_km?: number; meta?: NetworkMeta }; status: MapStatus }
export interface PlaceHit { name: string; type: string; lat: number; lon: number; bbox: number[] }

export interface Booking {
  id: number; status: string; patient_name: string; pickup_lat: number; pickup_lon: number;
  pickup_node: string | null; hospital_id: string; hospital_name: string | null; ambulance_id: string | null;
  eta_s: number | null; failure_reason: string | null; created_at: string; completed_at: string | null;
  run_id: number; live: JourneyState | null;
}

export const fmtTime = (s: number | null | undefined): string => {
  if (s === null || s === undefined || Number.isNaN(s)) return "–";
  const m = Math.floor(s / 60);
  const r = Math.round(s % 60);
  return m > 0 ? `${m}m ${r.toString().padStart(2, "0")}s` : `${r}s`;
};

export const STATUS_LABEL: Record<string, string> = {
  WAITING_FOR_AMBULANCE: "Waiting for ambulance",
  ASSIGNED: "Assigned",
  EN_ROUTE_TO_PICKUP: "En route to pickup",
  AT_PICKUP: "At pickup",
  TRANSPORTING: "Transporting to hospital",
  COMPLETED: "Arrived at hospital",
  CANCELLED: "Cancelled",
  FAILED: "Failed",
  REJECTED: "Rejected",
  AVAILABLE: "Available",
  RETURNING: "Returning to base",
  AT_HOSPITAL: "At hospital",
};

// ---------------------------------------------------------------- video analytics
export interface VideoStats {
  t: number; vehicles: number; by_class: Record<string, number>; queue: number; queue_length_px: number;
  queue_length_m: number | null; occupancy: number; mean_speed: number | null; speed_unit: string;
  congestion: "free" | "moderate" | "heavy" | "jam"; crossings_total: number; crossings_by_dir: Record<string, number>;
  flow_per_min: number; tracks_total: number; ambulances?: number;
}
export interface ZoneCfg { roi?: number[][]; line?: number[][]; meters_per_pixel?: number | null; stop_speed_frac?: number }
export interface VideoSourceInfo {
  id: string; name: string; kind: "file" | "url" | "webcam" | "push"; uri: string; mode: "live" | "batch"; loop: boolean;
  detector: string; conf: number; target_fps: number; classes: string[] | null; zone: ZoneCfg; detect?: "vehicles" | "road" | "all";
  objects_now?: Record<string, number>; detections_raw?: number; imgsz?: number; reconnects?: number;
  attach: { node?: string; field?: string; approach: string } | null; status: string; error: string | null; progress: number | null;
  proc_fps: number; video_fps: number | null; frame_size: [number, number] | null; frames_processed: number;
  detector_info: { name: string; weights?: string; conf: number } | null; stats: VideoStats | null;
  vehicles_by_class_total: Record<string, number>; events: { type: string; t: number; class?: string; track?: number; conf?: number; reasons?: string[] }[];
  ambulances_now?: { track: number; score: number; reasons: string[]; in_zone: boolean; passed: boolean }[];
  ambulance_model?: boolean; backend?: string | null; ambulance?: Record<string, unknown>;
  outputs: Record<string, string | null>; timeline?: VideoTimelineRow[]; summary?: Record<string, unknown>;
}
export interface VideoTimelineRow { t: number; vehicles: number; queue: number; flow_per_min: number; occupancy: number; mean_speed: number | null; crossings: number; congestion: string }
export interface VideoCaps { detectors: { yolo: boolean; yoloe?: boolean; resq?: boolean; "opencv-dnn": boolean; motion: boolean; weights_present: boolean }; samples: string[] }
export interface CameraSummary { id: string; name: string; kind: string; status: string; attach: { node?: string; field?: string; approach: string } | null; proc_fps: number; vehicles: number | null; queue: number | null; congestion: string | null; flow_per_min: number | null }
export const DEFAULT_ROI = [[0, 0.3], [1, 0.3], [1, 1], [0, 1]];
export const DEFAULT_LINE = [[0, 0.7], [1, 0.7]];

// ---------------------------------------------------------------- field signals
export type Lamp = "G" | "Y" | "R" | "X";
export interface FieldApproach { id: string; name: string; bearing: number; lanes: number }
export interface FieldRequest {
  owner: string; approach: string; trigger: string; requested_at: number; granted_at: number | null;
  released_at: number | null; status: string; hold_s: number | null; detail: Record<string, unknown>;
}
export interface FieldEvent { type: string; junction: string; wall: number; [k: string]: unknown }
export interface FieldJunction {
  id: string; name: string; lat: number; lon: number; approaches: FieldApproach[]; groups: string[][];
  plan: Record<string, number>; mode: "control" | "mirror"; driver: Record<string, unknown>; source: Record<string, unknown>;
  corridor: Record<string, number>; states: Record<string, Lamp>; remaining: Record<string, number>; mode_now: string;
  stage: string | null; current_group: number | null; elapsed: number | null; active: FieldRequest | null;
  pending: FieldRequest[]; history: FieldRequest[]; events: FieldEvent[]; camera_queues: Record<string, number | null>;
  driver_status: { type: string; error: string | null; sent: number; last: string | null };
  source_status: { type: string; fresh: boolean; error: string | null; messages: number; latest: Record<string, { state: string; raw: unknown }> } | null;
  violations: number; compensation: Record<string, number>;
}
export interface FieldAmbulance { id: string; lat: number; lon: number; speed: number | null; heading: number | null; age_s: number; simulated: boolean; corridor: Record<string, { approach: string; min_d: number }> }
export interface FieldSnapshot { type: "field"; junctions: FieldJunction[]; ambulances: FieldAmbulance[]; events: FieldEvent[]; follow_sim: boolean }
