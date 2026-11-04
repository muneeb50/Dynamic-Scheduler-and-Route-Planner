import { Suspense, lazy, useEffect, useState } from "react";
import { NetworkInfo, api } from "./api";
import { LinkBadge } from "./components/Panels";
import { useSimulation } from "./hooks/useSimulation";
import LivePage from "./pages/LivePage";
// other pages load on first use: a smaller first download, faster start
const AnalyticsPage = lazy(() => import("./pages/AnalyticsPage"));
const BookingsPage = lazy(() => import("./pages/BookingsPage"));
const CVPage = lazy(() => import("./pages/CVPage"));
const IntersectionsPage = lazy(() => import("./pages/IntersectionsPage"));
const PatientPage = lazy(() => import("./pages/PatientPage"));
const VideoPage = lazy(() => import("./pages/VideoPage"));
const SignalsPage = lazy(() => import("./pages/SignalsPage"));

const TABS = [
  ["live", "Live map"],
  ["intersections", "Intersections"],
  ["bookings", "Bookings"],
  ["analytics", "Analytics"],
  ["video", "Video AI"],
  ["signals", "Signals"],
  ["cv", "Sim cameras"],
  ["request", "Request ambulance"],
] as const;
type Tab = (typeof TABS)[number][0];

function readHash(): Tab {
  const h = window.location.hash.replace("#", "") as Tab;
  return TABS.some(([k]) => k === h) ? h : "live";
}

export default function App() {
  const [tab, setTab] = useState<Tab>(readHash());
  const [network, setNetwork] = useState<NetworkInfo | null>(null);
  const [netError, setNetError] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const { snap, link, events } = useSimulation();

  const loadNetwork = () =>
    api<NetworkInfo>("/api/network")
      .then((n) => {
        setNetwork(n);
        setNetError(null);
      })
      .catch((e: Error) => {
        setNetError(e.message);
        window.setTimeout(loadNetwork, 3000);
      });
  useEffect(() => {
    loadNetwork();
    const onHash = () => setTab(readHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  // a new map area was loaded on the server: fetch the new roads
  useEffect(() => {
    if (network && snap?.network_version && snap.network_version !== network.network_version) {
      setSelectedNode(null);
      loadNetwork();
    }
  }, [snap?.network_version]); // eslint-disable-line react-hooks/exhaustive-deps
  const mapState = snap?.map?.state;

  const go = (t: Tab) => {
    window.location.hash = t;
    setTab(t);
  };
  const openIntersection = (id: string) => {
    setSelectedNode(id);
    go("intersections");
  };

  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <span className="logo">✚</span>
          <div>
            <div className="brand-name">ResQRoute AI</div>
            <div className="brand-sub">{network ? `${network.name} · ${network.source === "osm" ? "OpenStreetMap" : "offline demo grid"}` : "control room"}</div>
          </div>
        </div>
        <nav>
          {TABS.map(([k, label]) => (
            <button key={k} className={`tab ${tab === k ? "active" : ""}`} onClick={() => go(k)}>{label}</button>
          ))}
        </nav>
        <LinkBadge link={link} />
      </header>
      <main>
        {network && mapState && ["queued", "downloading", "building"].includes(mapState) && (
          <div className="map-banner busy">🗺️ {snap?.map?.message || "Loading the real map…"} The current map keeps running and is replaced automatically when the new one is ready.</div>
        )}
        {network && mapState === "error" && network.source !== "osm" && (
          <div className="map-banner error">🗺️ Real map not loaded: {snap?.map?.message} Showing the offline demo grid. Open <b>Live map → Map area</b> to retry, pick another area or upload an .osm file.</div>
        )}
        {!network && (
          <div className="card center">
            {netError ? <>Cannot reach the backend ({netError}). Is <code>uvicorn app.main:app</code> running on port 8000? Retrying…</> : "Loading network…"}
          </div>
        )}
        <Suspense fallback={<div className="card muted">Loading…</div>}>
        {network && tab === "live" && <LivePage network={network} snap={snap} link={link} events={events} onOpenIntersection={openIntersection} />}
        {network && tab === "intersections" && <IntersectionsPage network={network} snap={snap} selected={selectedNode} onSelect={setSelectedNode} />}
        {network && tab === "bookings" && <BookingsPage network={network} snap={snap} />}
        {network && tab === "analytics" && <AnalyticsPage />}
        {network && tab === "video" && <VideoPage network={network} />}
        {network && tab === "signals" && <SignalsPage network={network} snap={snap} onOpenIntersection={openIntersection} />}
        {network && tab === "cv" && <CVPage network={network} snap={snap} />}
        {network && tab === "request" && <PatientPage network={network} snap={snap} />}
        </Suspense>
      </main>
    </div>
  );
}
