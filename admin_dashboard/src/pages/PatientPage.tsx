import { useEffect, useState } from "react";
import { Booking, JourneyState, LatLon, NetworkInfo, SimSnapshot, fmtTime, post, wsUrl } from "../api";
import LiveMap from "../components/LiveMap";
import { StatusPill } from "../components/Panels";

/** Browser version of the patient app (same API as the Flutter app) – handy for demos. */
export default function PatientPage({ network, snap }: { network: NetworkInfo; snap: SimSnapshot | null }) {
  const [name, setName] = useState("Demo Patient");
  const [phone, setPhone] = useState("9876543210");
  const [hospital, setHospital] = useState(network.hospitals[0]?.id ?? "");
  const [pickup, setPickup] = useState<LatLon | null>(null);
  const [booking, setBooking] = useState<Booking | null>(null);
  const [live, setLive] = useState<JourneyState | null>(null);
  const [stale, setStale] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!booking) return;
    let ws: WebSocket | null = null;
    let last = Date.now();
    let closed = false;
    let retry: number | undefined;
    const connect = () => {
      ws = new WebSocket(wsUrl(`/ws/bookings/${booking.id}`));
      ws.onmessage = (m) => {
        const msg = JSON.parse(m.data);
        last = Date.now();
        setStale(false);
        if (msg.booking) setLive(msg.booking);
      };
      ws.onclose = () => {
        if (!closed) retry = window.setTimeout(connect, 2000);
      };
    };
    connect();
    const t = window.setInterval(() => setStale(Date.now() - last > 3000), 1000);
    return () => {
      closed = true;
      window.clearInterval(t);
      window.clearTimeout(retry);
      ws?.close();
    };
  }, [booking]);

  const request = async () => {
    if (!pickup) return setError("Tap the map to choose a pickup location.");
    setError(null);
    try {
      const b = await post<Booking>("/api/bookings", {
        patient_name: name, phone, pickup_lat: pickup[0], pickup_lon: pickup[1], hospital_id: hospital,
      });
      setBooking(b);
      setLive(b.live);
    } catch (e) {
      setError((e as Error).message);
    }
  };
  const cancel = async () => {
    if (booking) await post(`/api/bookings/${booking.id}/cancel`).catch((e) => setError((e as Error).message));
  };

  const status = live?.status ?? booking?.status;
  const done = status && ["COMPLETED", "CANCELLED", "FAILED"].includes(status);
  return (
    <div className="live-layout">
      <div className="map-wrap">
        <LiveMap network={network} snap={snap} onPick={booking && !done ? undefined : setPickup} picked={booking ? null : pickup}
          focusJourney={live?.id ?? null} showVehicles />
      </div>
      <aside className="side">
        {!booking || done ? (
          <div className="card">
            <div className="card-title">Request an ambulance</div>
            <label className="field">Patient name<input value={name} onChange={(e) => setName(e.target.value)} /></label>
            <label className="field">Phone<input value={phone} onChange={(e) => setPhone(e.target.value)} /></label>
            <label className="field">Hospital
              <select value={hospital} onChange={(e) => setHospital(e.target.value)}>
                {network.hospitals.map((h) => <option key={h.id} value={h.id}>{h.name}</option>)}
              </select>
            </label>
            <div className="muted small">
              Pickup: {pickup ? `${pickup[0].toFixed(5)}, ${pickup[1].toFixed(5)}` : "tap the map"}
            </div>
            <button className="btn big" onClick={request}>Request ambulance</button>
            {booking && done && <button className="btn ghost" onClick={() => { setBooking(null); setLive(null); }}>New request</button>}
            {error && <div className="error">{error}</div>}
          </div>
        ) : null}
        {booking && (
          <div className="card">
            <div className="card-title">Booking #{booking.id}</div>
            <div className="row gap wrap">
              {status && <StatusPill status={status} />}
              {stale && <span className="link link-stale">Tracking data is stale – reconnecting</span>}
            </div>
            <div className="big-eta">{status === "COMPLETED" ? "Arrived" : status === "AT_PICKUP" ? "Boarding" : live?.eta_s != null ? fmtTime(live.eta_s) : "–"}</div>
            <div className="muted small">
              {status === "EN_ROUTE_TO_PICKUP" && "Ambulance on the way to you"}
              {status === "AT_PICKUP" && "Ambulance has arrived at the pickup point"}
              {status === "TRANSPORTING" && "On the way to hospital"}
              {status === "WAITING_FOR_AMBULANCE" && "All ambulances are busy – you are in the queue"}
              {live?.failure_reason && <span className="error"> {live.failure_reason}</span>}
            </div>
            {live?.ambulance && (
              <div className="small" style={{ marginTop: 8 }}>
                {live.ambulance.name} ({live.ambulance.id}) · {(live.ambulance.speed * 3.6).toFixed(0)} km/h
              </div>
            )}
            {!done && <button className="btn danger" style={{ marginTop: 12 }} onClick={cancel}>Cancel booking</button>}
            {live?.metrics && (
              <div className="small" style={{ marginTop: 10 }}>
                Total {fmtTime(live.metrics.total_time_s)} · {live.metrics.priority_granted} green lights granted ·
                {" "}{live.metrics.signal_delay_s}s waiting at signals
              </div>
            )}
          </div>
        )}
      </aside>
    </div>
  );
}
