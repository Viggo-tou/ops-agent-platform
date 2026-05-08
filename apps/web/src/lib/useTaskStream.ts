import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { api } from "./api";

export interface TaskStreamState {
  /** EventSource is open and we've received the snapshot. */
  connected: boolean;
  /** Server signaled terminal state. Stream is closed; no further updates. */
  done: boolean;
  /** Server signaled awaiting_approval. Stream stays open at slow tick. */
  paused: boolean;
  /** ms since the most recent server message (heartbeat counts). */
  lastEventAgoMs: number | null;
  /** Most recent error reason, if any. */
  error: string | null;
}

/**
 * Subscribe to /api/tasks/{taskId}/events/stream.
 *
 * Events trigger react-query invalidation for the task / events / tool-executions
 * caches, so existing panels refresh without changes.
 *
 * Returns a small status object for UI indicators (live dot, paused banner, etc).
 */
export function useTaskStream(taskId: string | undefined): TaskStreamState {
  const queryClient = useQueryClient();
  const [connected, setConnected] = useState(false);
  const [done, setDone] = useState(false);
  const [paused, setPaused] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastTickAt, setLastTickAt] = useState<number | null>(null);
  const [now, setNow] = useState<number>(Date.now());

  const esRef = useRef<EventSource | null>(null);
  const doneRef = useRef(false);

  useEffect(() => {
    if (!taskId) return;
    doneRef.current = false;

    const url = `${api.baseUrl}/tasks/${encodeURIComponent(taskId)}/events/stream`;
    // EventSource doesn't support custom headers natively; the dev backend doesn't
    // require auth on the stream, so we connect directly. In prod with auth-gated
    // SSE, we'd switch to fetch + ReadableStream + manual SSE parsing.
    const es = new EventSource(url, { withCredentials: false });
    esRef.current = es;
    setConnected(false);
    setDone(false);
    setPaused(false);
    setError(null);

    const tick = () => setLastTickAt(Date.now());

    es.addEventListener("snapshot", () => {
      setConnected(true);
      tick();
      // Snapshot is authoritative — re-fetch the canonical query state.
      void queryClient.invalidateQueries({ queryKey: ["task", taskId] });
      void queryClient.invalidateQueries({ queryKey: ["task-events", taskId] });
      void queryClient.invalidateQueries({ queryKey: ["task-tool-executions", taskId] });
    });

    es.addEventListener("log", () => {
      tick();
      // New event row → at minimum the events list changed; the task header
      // (status / pending_approval) may have changed too. Cheap to invalidate
      // both since react-query dedupes in-flight fetches.
      void queryClient.invalidateQueries({ queryKey: ["task", taskId] });
      void queryClient.invalidateQueries({ queryKey: ["task-events", taskId] });
      void queryClient.invalidateQueries({ queryKey: ["task-tool-executions", taskId] });
    });

    es.addEventListener("status", () => {
      tick();
      void queryClient.invalidateQueries({ queryKey: ["task", taskId] });
      void queryClient.invalidateQueries({ queryKey: ["task-events", taskId] });
    });

    es.addEventListener("paused", () => {
      tick();
      setPaused(true);
      void queryClient.invalidateQueries({ queryKey: ["task", taskId] });
    });

    es.addEventListener("heartbeat", () => {
      tick();
      // Resume is "no longer paused" — the server doesn't send an explicit
      // "unpaused" event; if the next heartbeat arrives after the status flips
      // we'll learn via the `status` listener.
    });

    es.addEventListener("done", () => {
      tick();
      doneRef.current = true;
      setDone(true);
      setPaused(false);
      void queryClient.invalidateQueries({ queryKey: ["task", taskId] });
      void queryClient.invalidateQueries({ queryKey: ["task-events", taskId] });
      void queryClient.invalidateQueries({ queryKey: ["task-tool-executions", taskId] });
      // Server will close the stream right after `done`. Close locally too so
      // EventSource doesn't auto-reconnect.
      es.close();
    });

    es.addEventListener("error" as any, (ev: any) => {
      // EventSource fires `error` on transient disconnects (it auto-reconnects).
      // Only surface to the user if `done` was already signaled — otherwise
      // ignore and let it reconnect.
      if (doneRef.current) return;
      const reason = ev?.data ? String(ev.data) : "stream interrupted";
      setError(reason);
      setConnected(false);
    });

    return () => {
      es.close();
      esRef.current = null;
    };
  }, [taskId, queryClient]);

  // Tick the relative-age counter once per second so the UI can display
  // "5s ago" / "live" without recomputing on every render.
  useEffect(() => {
    if (done || !connected) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [done, connected]);

  const lastEventAgoMs = lastTickAt == null ? null : Math.max(0, now - lastTickAt);

  return { connected, done, paused, lastEventAgoMs, error };
}
