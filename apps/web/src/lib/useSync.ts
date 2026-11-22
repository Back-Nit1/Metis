/**
 * One sync Web Worker per session. Screens subscribe to the entity kinds they
 * show and re-read only those; nothing polls on the main thread.
 *
 * A `reset` notice reaches every subscriber: the server could not continue
 * from our cursor, so everything shown may be stale.
 */
import { createContext, createElement, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import type { QueueStats } from "../contracts/metis";
import { subscribeAuthToken, syncCredentials } from "./api";
import type { ChangeNotice, SyncStatus, WorkerIn, WorkerOut } from "./syncCore";

/** `unsupported`: no Web Worker here, so nothing refreshes on its own. */
export type SyncConnection = SyncStatus | "unsupported";

type Listener = { kinds: ReadonlySet<string>; cb: (notice: ChangeNotice) => void };

type SyncValue = {
  status: SyncConnection;
  retryInMs: number | null;
  queue: QueueStats | null;
  subscribe: (kinds: readonly string[], cb: (notice: ChangeNotice) => void) => () => void;
};

const SyncContext = createContext<SyncValue | null>(null);

export function SyncProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<SyncConnection>(() => (typeof Worker === "undefined" ? "unsupported" : "connecting"));
  const [retryInMs, setRetryInMs] = useState<number | null>(null);
  const [queue, setQueue] = useState<QueueStats | null>(null);
  const listeners = useRef(new Set<Listener>());

  useEffect(() => {
    const creds = syncCredentials();
    if (typeof Worker === "undefined" || !creds) return;
    const worker = new Worker(new URL("./sync.worker.ts", import.meta.url), { type: "module" });
    const send = (message: WorkerIn) => worker.postMessage(message);
    worker.onmessage = (event: MessageEvent<WorkerOut>) => {
      const message = event.data;
      if (message.type === "status") {
        setStatus(message.status);
        setRetryInMs(message.retryInMs);
        return;
      }
      if (message.queue) setQueue(message.queue);
      for (const l of listeners.current) {
        if (message.reset || message.kinds.some((k) => l.kinds.has(k))) l.cb(message);
      }
    };
    send({ type: "start", ...creds });
    const unsubscribe = subscribeAuthToken(() => send({ type: "token", token: syncCredentials()?.token ?? null }));
    return () => {
      unsubscribe();
      send({ type: "stop" });
      worker.terminate();
    };
  }, []);

  const subscribe = useCallback((kinds: readonly string[], cb: (notice: ChangeNotice) => void) => {
    const listener: Listener = { kinds: new Set(kinds), cb };
    listeners.current.add(listener);
    return () => void listeners.current.delete(listener);
  }, []);

  const value = useMemo<SyncValue>(() => ({ status, retryInMs, queue, subscribe }), [status, retryInMs, queue, subscribe]);
  return createElement(SyncContext.Provider, { value }, children);
}

export function useSync(): SyncValue {
  const value = useContext(SyncContext);
  if (!value) throw new Error("useSync must be used inside SyncProvider");
  return value;
}

/** Calls `onChange` when any of `kinds` changes (or on a reset). `kinds` is read once per join of its values. */
export function useChanges(kinds: readonly string[], onChange: (notice: ChangeNotice) => void): void {
  const { subscribe } = useSync();
  const latest = useRef(onChange);
  latest.current = onChange;
  const key = kinds.join(",");
  useEffect(() => subscribe(key.split(","), (n) => latest.current(n)), [subscribe, key]);
}
