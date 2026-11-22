/// <reference lib="webworker" />
/**
 * Dedicated Web Worker: holds the change-feed long-poll off the UI thread.
 * The API base and bearer arrive by postMessage and live only in this worker's
 * memory — never storage or a URL.
 */
import { SyncLoop, type WorkerIn } from "./syncCore";

const scope = self as unknown as DedicatedWorkerGlobalScope;

const loop = new SyncLoop({
  fetch: (url, init) => fetch(url, init),
  post: (message) => scope.postMessage(message),
  now: () => Date.now(),
  schedule: (fn, ms) => setTimeout(fn, ms),
  sleep: (ms, signal) =>
    new Promise<void>((resolve) => {
      const timer = setTimeout(resolve, ms);
      signal.addEventListener("abort", () => (clearTimeout(timer), resolve()), { once: true });
    }),
});

scope.onmessage = (event: MessageEvent<WorkerIn>) => loop.handle(event.data);
