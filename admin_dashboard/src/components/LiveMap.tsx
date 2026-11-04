import { Fragment, useEffect, useMemo, useRef } from "react";
import L from "leaflet";
import { GeoJSON, MapContainer, Marker, Polyline, Rectangle, TileLayer, Tooltip, useMap, useMapEvents } from "react-leaflet";
import "leaflet/dist/leaflet.css";
import { IntersectionInfo, LatLon, NetworkInfo, SimSnapshot, SignalState } from "../api";

const SIGNAL_COLORS: Record<string, string> = { G: "#30a46c", Y: "#f5d90a", R: "#e5484d" };
const MODE_RING: Record<string, string> = { NORMAL: "#8b8d98", PRIORITY: "#8e4ec6", RECOVERY: "#f76b15" };
const ROAD_WEIGHT: Record<string, number> = { motorway: 6, trunk: 6, primary: 5, secondary: 4, tertiary: 3.5 };

const icon = (html: string, size = 26) =>
  L.divIcon({ html, className: "map-icon", iconSize: [size, size], iconAnchor: [size / 2, size / 2] });

const hospitalIcon = icon('<div class="pin pin-hospital">H</div>');
const baseIcon = icon('<div class="pin pin-base">⌂</div>', 22);
const pickupIcon = icon('<div class="pin pin-pickup">●</div>', 22);
const ambulanceIcon = (busy: boolean) => icon(`<div class="pin pin-amb ${busy ? "busy" : ""}">✚</div>`, 28);
const testAmbIcon = icon('<div class="pin pin-amb busy test">✚</div>', 24);

type Vehicle = [number, number, number, number, number?];

/** A canvas overlay redrawn from data – thousands of vehicles and hundreds of signals stay fast. */
class CanvasOverlay extends L.Layer {
  private canvas!: HTMLCanvasElement;
  private drawFn: (ctx: CanvasRenderingContext2D, map: L.Map) => void;
  private lmap?: L.Map;
  constructor(draw: (ctx: CanvasRenderingContext2D, map: L.Map) => void, private z = 400) {
    super();
    this.drawFn = draw;
  }
  setDraw(draw: (ctx: CanvasRenderingContext2D, map: L.Map) => void) {
    this.drawFn = draw;
    this.redraw();
  }
  onAdd(map: L.Map) {
    this.lmap = map;
    this.canvas = L.DomUtil.create("canvas", "leaflet-canvas-overlay") as HTMLCanvasElement;
    this.canvas.style.position = "absolute";
    this.canvas.style.pointerEvents = "none";
    this.canvas.style.zIndex = String(this.z);
    map.getPanes().overlayPane.appendChild(this.canvas);
    map.on("moveend zoomend resize viewreset", this.reset, this);
    map.on("zoomstart", this.hide, this);
    this.reset();
    return this;
  }
  onRemove(map: L.Map) {
    map.off("moveend zoomend resize viewreset", this.reset, this);
    map.off("zoomstart", this.hide, this);
    this.canvas.remove();
    return this;
  }
  private hide() {
    if (this.canvas) this.canvas.style.visibility = "hidden";
  }
  private reset() {
    const map = this.lmap;
    if (!map || !this.canvas) return;
    const size = map.getSize();
    const dpr = window.devicePixelRatio || 1;
    L.DomUtil.setPosition(this.canvas, map.containerPointToLayerPoint([0, 0]));
    this.canvas.width = size.x * dpr;
    this.canvas.height = size.y * dpr;
    this.canvas.style.width = `${size.x}px`;
    this.canvas.style.height = `${size.y}px`;
    this.canvas.style.visibility = "visible";
    this.redraw();
  }
  redraw() {
    const map = this.lmap;
    if (!map || !this.canvas) return;
    const ctx = this.canvas.getContext("2d")!;
    const dpr = window.devicePixelRatio || 1;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    this.drawFn(ctx, map);
  }
}

function useCanvasLayer(draw: (ctx: CanvasRenderingContext2D, map: L.Map) => void, deps: unknown[], z = 400) {
  const map = useMap();
  const layer = useRef<CanvasOverlay | null>(null);
  useEffect(() => {
    layer.current = new CanvasOverlay(draw, z);
    layer.current.addTo(map);
    return () => {
      layer.current?.remove();
      layer.current = null;
    };
  }, [map]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    layer.current?.setDraw(draw);
  }, deps); // eslint-disable-line react-hooks/exhaustive-deps
  return layer;
}

/** Fast linear lat/lon -> screen mapping for the current view (exact enough within a city). */
function linearProjector(map: L.Map) {
  const c = map.getCenter();
  const p0 = map.latLngToContainerPoint(c);
  const p1 = map.latLngToContainerPoint([c.lat + 0.01, c.lng + 0.01]);
  const kx = (p1.x - p0.x) / 0.01;
  const ky = (p1.y - p0.y) / 0.01;
  return (lat: number, lon: number): [number, number] => [p0.x + (lon - c.lng) * kx, p0.y + (lat - c.lat) * ky];
}

const ANIM_MAX_VEHICLES = 20000;

/** Vehicles drawn on canvas and animated at the display's frame rate between server snapshots (2 per
 *  second): each vehicle glides from where it was drawn to its new position, so motion looks smooth. */
function VehicleLayer({ vehicles }: { vehicles: Vehicle[] }) {
  const state = useRef({
    from: new Map<number, [number, number]>(),
    to: new Map<number, [number, number]>(),
    t0: 0, dur: 500, lastArrival: 0, raf: 0, list: [] as Vehicle[],
  });
  const layer = useCanvasLayer((ctx, map) => {
    const st = state.current;
    const zoom = map.getZoom();
    const r = zoom >= 17 ? 3.6 : zoom >= 15 ? 2.6 : zoom >= 13 ? 1.7 : 1.1;
    const size = map.getSize();
    const proj = linearProjector(map);
    const f = Math.min(1, (performance.now() - st.t0) / st.dur);
    const moving = new Path2D();
    const stopped = new Path2D();
    for (const v of st.list) {
      if (v[3] !== 0) continue;
      let lat = v[0], lon = v[1];
      const id = v[4];
      if (id !== undefined && f < 1) {
        const a = st.from.get(id);
        if (a) {
          lat = a[0] + (v[0] - a[0]) * f;
          lon = a[1] + (v[1] - a[1]) * f;
        }
      }
      const [x, y] = proj(lat, lon);
      if (x < -5 || y < -5 || x > size.x + 5 || y > size.y + 5) continue;
      const path = v[2] < 0.5 ? stopped : moving;
      path.moveTo(x + r, y);
      path.arc(x, y, r, 0, Math.PI * 2);
    }
    ctx.fillStyle = "#3e63dd";
    ctx.fill(moving);
    ctx.fillStyle = "#f76b15";
    ctx.fill(stopped);
  }, [], 410);

  useEffect(() => {
    const st = state.current;
    const now = performance.now();
    // where each vehicle is drawn right now becomes the start of the next glide
    const f = Math.min(1, (now - st.t0) / st.dur);
    const from = new Map<number, [number, number]>();
    for (const v of st.list) {
      const id = v[4];
      if (id === undefined) continue;
      const a = st.from.get(id);
      from.set(id, a && f < 1 ? [a[0] + (v[0] - a[0]) * f, a[1] + (v[1] - a[1]) * f] : [v[0], v[1]]);
    }
    if (st.lastArrival) st.dur = Math.min(1500, Math.max(150, now - st.lastArrival));
    st.lastArrival = now;
    st.from = from;
    st.list = vehicles;
    st.t0 = now;
    cancelAnimationFrame(st.raf);
    const animate = vehicles.length <= ANIM_MAX_VEHICLES && vehicles[0]?.[4] !== undefined;
    const tick = () => {
      layer.current?.redraw();
      if (animate && performance.now() - st.t0 < st.dur) st.raf = requestAnimationFrame(tick);
    };
    tick();
    return () => cancelAnimationFrame(st.raf);
  }, [vehicles]); // eslint-disable-line react-hooks/exhaustive-deps
  return null;
}

function SignalLayer({ intersections, signals, selected, priorityNodes }: {
  intersections: IntersectionInfo[]; signals?: Record<string, SignalState>; selected?: string | null; priorityNodes: Set<string>;
}) {
  useCanvasLayer((ctx, map) => {
    const zoom = map.getZoom();
    const b = map.getBounds().pad(0.1);
    const detailed = zoom >= 15;
    for (const i of intersections) {
      if (!b.contains([i.lat, i.lon])) continue;
      const sig = signals?.[i.id];
      if (detailed && sig) {
        ctx.lineWidth = zoom >= 17 ? 8 : 6;
        ctx.lineCap = "round";
        for (const [a, pts] of Object.entries(i.approaches)) {
          if (pts.length < 2) continue;
          const p0 = map.latLngToContainerPoint(pts[0] as L.LatLngTuple);
          const p1 = map.latLngToContainerPoint(pts[pts.length - 1] as L.LatLngTuple);
          ctx.strokeStyle = SIGNAL_COLORS[sig.approaches[a] ?? "R"];
          ctx.beginPath();
          ctx.moveTo(p0.x, p0.y);
          ctx.lineTo(p1.x, p1.y);
          ctx.stroke();
        }
      }
      const c = map.latLngToContainerPoint([i.lat, i.lon]);
      const mode = sig?.mode ?? "NORMAL";
      const r = (selected === i.id ? 10 : detailed ? 7 : 4.5) + (mode !== "NORMAL" ? 1.5 : 0);
      ctx.beginPath();
      ctx.arc(c.x, c.y, r, 0, Math.PI * 2);
      ctx.fillStyle = priorityNodes.has(i.id) ? "#8e4ec6" : "#11131a";
      ctx.fill();
      ctx.lineWidth = mode !== "NORMAL" ? 3.5 : selected === i.id ? 3 : 1.6;
      ctx.strokeStyle = selected === i.id && mode === "NORMAL" ? "#ffffff" : MODE_RING[mode];
      ctx.stroke();
      if (!detailed && sig) { // small centre dot with the main approach state when zoomed out
        const anyGreen = Object.values(sig.approaches).includes("G");
        ctx.beginPath();
        ctx.arc(c.x, c.y, 1.8, 0, Math.PI * 2);
        ctx.fillStyle = anyGreen ? SIGNAL_COLORS.G : SIGNAL_COLORS.R;
        ctx.fill();
      }
    }
  }, [intersections, signals, selected, priorityNodes], 420);
  return null;
}

/** Click / hover on a junction (canvas layers have no DOM elements of their own). */
function JunctionPicker({ intersections, signals, onSelectNode, onPick, names }: {
  intersections: IntersectionInfo[]; signals?: Record<string, SignalState>; onSelectNode?: (id: string) => void;
  onPick?: (p: LatLon) => void; names: Record<string, string>;
}) {
  const tip = useRef<L.Tooltip | null>(null);
  const nearest = (map: L.Map, ll: L.LatLng, px = 12) => {
    const p = map.latLngToContainerPoint(ll);
    let best: IntersectionInfo | null = null;
    let bd = px;
    for (const i of intersections) {
      const q = map.latLngToContainerPoint([i.lat, i.lon]);
      const d = Math.hypot(q.x - p.x, q.y - p.y);
      if (d < bd) {
        bd = d;
        best = i;
      }
    }
    return best;
  };
  const map = useMapEvents({
    click: (e) => {
      const hit = onSelectNode ? nearest(map, e.latlng) : null;
      if (hit) onSelectNode!(hit.id);
      else onPick?.([e.latlng.lat, e.latlng.lng]);
    },
    mousemove: (e) => {
      const hit = nearest(map, e.latlng, 10);
      map.getContainer().style.cursor = hit && onSelectNode ? "pointer" : "";
      if (!hit) {
        tip.current?.remove();
        tip.current = null;
        return;
      }
      const sig = signals?.[hit.id];
      const html = `<b>${names[hit.id] ?? hit.name}</b>${hit.signal_source === "inferred" ? " <i>(signal inferred)</i>" : ""}` +
        (sig ? `<br/>${sig.mode} · ${sig.stage} (phase ${sig.current_group + 1})<br/>queues: ${Object.values(sig.queues).map((q) => Math.round(q)).join(" / ")}` : "");
      if (!tip.current) tip.current = L.tooltip({ direction: "top", offset: [0, -8] }).setLatLng([hit.lat, hit.lon]).setContent(html).addTo(map);
      else tip.current.setLatLng([hit.lat, hit.lon]).setContent(html);
    },
    mouseout: () => {
      tip.current?.remove();
      tip.current = null;
    },
  });
  return null;
}

function FitBounds({ bbox }: { bbox: [number, number, number, number] }) {
  const map = useMap();
  useEffect(() => {
    map.fitBounds([[bbox[0], bbox[1]], [bbox[2], bbox[3]]], { padding: [20, 20] });
  }, [map, bbox[0], bbox[1], bbox[2], bbox[3]]); // eslint-disable-line react-hooks/exhaustive-deps
  return null;
}

function BoundsReporter({ onBounds }: { onBounds?: (b: [number, number, number, number]) => void }) {
  const map = useMapEvents({
    moveend: () => {
      const b = map.getBounds();
      onBounds?.([b.getSouth(), b.getWest(), b.getNorth(), b.getEast()]);
    },
  });
  useEffect(() => {
    const b = map.getBounds();
    onBounds?.([b.getSouth(), b.getWest(), b.getNorth(), b.getEast()]);
  }, [map]); // eslint-disable-line react-hooks/exhaustive-deps
  return null;
}

export interface LiveMapProps {
  network: NetworkInfo;
  snap: SimSnapshot | null;
  selectedNode?: string | null;
  onSelectNode?: (id: string) => void;
  onPick?: (p: LatLon) => void;
  picked?: LatLon | null;
  focusJourney?: string | null;
  showVehicles?: boolean;
  compact?: boolean;
  onBounds?: (b: [number, number, number, number]) => void;
  previewBox?: number[] | null;
}

export default function LiveMap({ network, snap, selectedNode, onSelectNode, onPick, picked, focusJourney,
  showVehicles = true, compact = false, onBounds, previewBox }: LiveMapProps) {
  const center: [number, number] = [(network.bbox[0] + network.bbox[2]) / 2, (network.bbox[1] + network.bbox[3]) / 2];
  const roads = useMemo(() => network.roads, [network]);
  const names = useMemo(() => Object.fromEntries(network.intersections.map((i) => [i.id, i.name])), [network]);
  const journeys = snap?.journeys ?? [];
  const priorityKey = (snap?.priority ?? []).map((p) => p.node).join(",");
  const priorityNodes = useMemo(() => new Set(priorityKey ? priorityKey.split(",") : []), [priorityKey]);
  const vehicles = useMemo(() => (showVehicles ? snap?.vehicles ?? [] : []), [snap?.vehicles, showVehicles]);

  return (
    <MapContainer center={center} zoom={15} preferCanvas className={compact ? "map compact" : "map"} zoomControl={!compact}>
      <TileLayer
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        opacity={network.source === "osm" ? 0.7 : 0.55}
        maxZoom={19}
      />
      <FitBounds bbox={network.bbox} />
      <BoundsReporter onBounds={onBounds} />
      <GeoJSON
        key={`${network.name}-${network.network_version ?? 0}`}
        data={roads}
        interactive={false}
        style={(f) => ({
          color: "#3b4252",
          weight: ROAD_WEIGHT[String(f?.properties?.class ?? "").replace("_link", "")] ?? ((f?.properties?.lanes ?? 1) > 1 ? 4 : 2.2),
          opacity: network.source === "osm" ? 0.55 : 0.75,
        })}
      />
      <VehicleLayer vehicles={vehicles} />
      <SignalLayer intersections={network.intersections} signals={snap?.signals} selected={selectedNode} priorityNodes={priorityNodes} />
      <JunctionPicker intersections={network.intersections} signals={snap?.signals} onSelectNode={onSelectNode} onPick={onPick} names={names} />
      {previewBox && previewBox.length === 4 && (
        <Rectangle bounds={[[previewBox[0], previewBox[1]], [previewBox[2], previewBox[3]]]}
          pathOptions={{ color: "#3e63dd", weight: 2, dashArray: "6 6", fillOpacity: 0.04 }} interactive={false} />
      )}

      {journeys
        .filter((j) => !focusJourney || j.id === focusJourney)
        .map((j) => (
          <Fragment key={j.id}>
            {j.route.length > 1 && (
              <Polyline positions={j.route} interactive={false} pathOptions={{ color: "#e5484d", weight: 5, opacity: 0.85, dashArray: "8 6" }} />
            )}
            <Marker position={j.pickup} icon={pickupIcon}>
              <Tooltip>Pickup · booking {j.id}</Tooltip>
            </Marker>
          </Fragment>
        ))}

      {network.hospitals.map((h) => (
        <Marker key={h.id} position={[h.lat, h.lon]} icon={hospitalIcon}>
          <Tooltip>{h.name}</Tooltip>
        </Marker>
      ))}
      {!compact &&
        network.bases.map((b) => (
          <Marker key={b.id} position={[b.lat, b.lon]} icon={baseIcon}>
            <Tooltip>{b.name}</Tooltip>
          </Marker>
        ))}
      {snap?.ambulances
        .filter((a) => !focusJourney || a.journey_id === focusJourney)
        .filter((a) => a.status !== "AVAILABLE" || !compact)
        .map((a) => (
          <Marker key={a.id} position={[a.lat, a.lon]} icon={ambulanceIcon(a.status !== "AVAILABLE")} zIndexOffset={1000}>
            <Tooltip>
              <b>{a.name}</b> ({a.id})<br />
              {a.status} · {(a.speed * 3.6).toFixed(0)} km/h
            </Tooltip>
          </Marker>
        ))}
      {!focusJourney && snap?.test_ambulances?.map((a) => (
        <Marker key={a.id} position={[a.lat, a.lon]} icon={testAmbIcon} zIndexOffset={1000}>
          <Tooltip>Test ambulance {a.id} → {names[a.node] ?? a.node} · {(a.speed * 3.6).toFixed(0)} km/h</Tooltip>
        </Marker>
      ))}
      {picked && (
        <Marker position={picked} icon={pickupIcon}>
          <Tooltip permanent direction="top">Pickup</Tooltip>
        </Marker>
      )}
    </MapContainer>
  );
}
