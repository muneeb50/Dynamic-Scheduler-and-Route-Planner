import { useEffect, useRef, useState } from "react";
import { SimEvent, SimSnapshot, wsUrl } from "../api";

export type LinkState = "connecting" | "live" | "stale" | "offline";

/** Subscribes to /ws/simulation with automatic reconnect and stale-data detection. */
export function useSimulation(): { snap: SimSnapshot | null; link: LinkState; events: SimEvent[] } {
  const [snap, setSnap] = useState<SimSnapshot | null>(null);
  const [link, setLink] = useState<LinkState>("connecting");
  const [events, setEvents] = useState<SimEvent[]>([]);
  const lastMsg = useRef<number>(0);

  useEffect(() => {
    let ws: WebSocket | null = null;
    let retry: number | undefined;
    let closed = false;
    let attempt = 0;

    const connect = () => {
      setLink("connecting");
      ws = new WebSocket(wsUrl("/ws/simulation"));
      ws.onopen = () => {
        attempt = 0;
      };
      ws.onmessage = (m) => {
        const data = JSON.parse(m.data) as SimSnapshot & { vehicles_q?: { o: [number, number]; n?: number; d: number[] } };
        if (data.vehicles_q) { // compact vehicle encoding from the server: n integers per vehicle
          const { o, d } = data.vehicles_q;
          const n = data.vehicles_q.n ?? 4;
          const out: SimSnapshot["vehicles"] = new Array(d.length / n);
          for (let i = 0, k = 0; i < d.length; i += n, k += 1) {
            out[k] = n >= 5 ? [o[0] + d[i] / 1e5, o[1] + d[i + 1] / 1e5, d[i + 2] / 10, d[i + 3], d[i + 4]]
              : [o[0] + d[i] / 1e5, o[1] + d[i + 1] / 1e5, d[i + 2] / 10, d[i + 3]];
          }
          data.vehicles = out;
          delete data.vehicles_q;
        }
        lastMsg.current = Date.now();
        setLink("live");
        setSnap(data);
        if (data.events?.length) {
          setEvents((prev) => [...data.events.slice().reverse(), ...prev].slice(0, 300));
        }
      };
      ws.onclose = () => {
        if (closed) return;
        setLink("offline");
        attempt += 1;
        retry = window.setTimeout(connect, Math.min(1000 * 2 ** attempt, 10000));
      };
      ws.onerror = () => ws?.close();
    };
    connect();
    const ping = window.setInterval(() => {
      if (ws?.readyState === WebSocket.OPEN) ws.send("ping");
      if (lastMsg.current && Date.now() - lastMsg.current > 3000) setLink((l) => (l === "live" ? "stale" : l));
    }, 1000);
    return () => {
      closed = true;
      window.clearInterval(ping);
      window.clearTimeout(retry);
      ws?.close();
    };
  }, []);

  return { snap, link, events };
}

export function usePoll<T>(fn: () => Promise<T>, ms: number, deps: unknown[] = []): { data: T | null; error: string | null; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let alive = true;
    const run = () =>
      fn()
        .then((d) => {
          if (alive) {
            setData(d);
            setError(null);
          }
        })
        .catch((e: Error) => alive && setError(e.message));
    run();
    const id = ms > 0 ? window.setInterval(run, ms) : undefined;
    return () => {
      alive = false;
      if (id) window.clearInterval(id);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  return { data, error, reload: () => setTick((t) => t + 1) };
}
