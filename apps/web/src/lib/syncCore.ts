/**
 * The change-feed loop the sync Web Worker runs, kept free of worker globals so
 * it can be tested with a fake fetch, clock and timer.
 *
 * It long-polls `GET /v1/changes?after=<cursor>&wait=25`, keeps the cursor in
 * memory, backs off (capped) when the server is unreachable and says so, and
 * hands the page at most one coalesced notice per `coalesceMs`. The first
 * request is a baseline (`wait=0`, repeated while it still returns old
 * history): the page did its own initial load, so the history before it is
 * skipped, not replayed. After a reset the cursor the server returns is used.
 */
import type { ChangesResponse, QueueStats } from "../contracts/metis";

export type SyncStatus = "connecting" | "live" | "offline" | "unauthorized" | "stopped";

export type WorkerIn =
  | { type: "start"; baseUrl: string; token: string | null; browserSession?: string }
  | { type: "stop" }
  | { type: "token"; token: string | null };

/** `reset` means the server could not continue from our cursor: refetch everything shown. */
export type ChangeNotice = { type: "changes"; kinds: string[]; ids: string[]; queue: QueueStats | null; reset: boolean };
export type StatusNotice = { type: "status"; status: SyncStatus; retryInMs: number | null };
export type WorkerOut = ChangeNotice | StatusNotice;

export const LONG_POLL_SECONDS = 25;
export const COALESCE_MS = 300;
export const BACKOFF_BASE_MS = 1000;
export const BACKOFF_CAP_MS = 30_000;
/** A held request that outlives the server's own wait by this much is treated as lost. */
const REQUEST_GRACE_MS = 10_000;

/** 1s, 2s, 4s … capped. `failures` counts consecutive failures, starting at 1. */
export function backoffMs(failures: number, base = BACKOFF_BASE_MS, cap = BACKOFF_CAP_MS): number {
  return Math.min(cap, base * 2 ** Math.max(0, failures - 1));
}

export function changesUrl(baseUrl: string, after: number, wait: number): string {
  return `${baseUrl.replace(/\/$/, "")}/v1/changes?after=${after}&wait=${wait}`;
}

/** Merges responses until the page is told; duplicates collapse. */
export class Coalescer {
  private kinds = new Set<string>();
  private ids = new Set<string>();
  private queue: QueueStats | null = null;
  private queueKey = "";
  private queueDirty = false;
  private reset = false;

  add(response: ChangesResponse): void {
    if (response.reset) this.reset = true;
    for (const change of response.changes ?? []) {
      this.kinds.add(change.kind);
      this.ids.add(change.id);
    }
    if (response.queue) {
      const key = JSON.stringify(response.queue);
      if (key !== this.queueKey) (this.queueKey = key), (this.queueDirty = true);
      this.queue = response.queue;
    }
  }

  get pending(): boolean {
    return this.reset || this.kinds.size > 0 || this.queueDirty;
  }

  /** The merged notice, or null when nothing new happened. Clears what it returns. */
  take(): ChangeNotice | null {
    if (!this.pending) return null;
    const notice: ChangeNotice = {
      type: "changes",
      kinds: [...this.kinds],
      ids: [...this.ids],
      queue: this.queue,
      reset: this.reset,
    };
    this.kinds.clear();
    this.ids.clear();
    this.queueDirty = false;
    this.reset = false;
    return notice;
  }
}

export type LoopDeps = {
  fetch: (url: string, init: RequestInit) => Promise<Response>;
  post: (message: WorkerOut) => void;
  now: () => number;
  /** Resolves after `ms`, or early (without error) when `signal` aborts. */
  sleep: (ms: number, signal: AbortSignal) => Promise<void>;
  schedule: (fn: () => void, ms: number) => unknown;
  coalesceMs?: number;
};

export class SyncLoop {
  cursor = 0;
  /** False until the baseline (history before the page loaded) has been skipped. */
  primed = false;
  private token: string | null = null;
  private browserSession: string | undefined;
  private baseUrl = "";
  private failures = 0;
  private status: SyncStatus = "stopped";
  private stopper: AbortController | null = null;
  private authWait: (() => void) | null = null;
  private coalescer = new Coalescer();
  private lastFlush = -Infinity;
  private flushQueued = false;

  constructor(private readonly deps: LoopDeps) {}

  handle(message: WorkerIn): void {
    if (message.type === "start") {
      this.stop();
      this.baseUrl = message.baseUrl;
      this.token = message.token;
      this.browserSession = message.browserSession;
      this.cursor = 0;
      this.primed = false;
      this.failures = 0;
      void this.run();
    } else if (message.type === "token") {
      this.token = message.token;
      this.authWait?.();
    } else {
      this.stop();
    }
  }

  stop(): void {
    this.stopper?.abort();
    this.stopper = null;
    this.authWait?.();
    this.setStatus("stopped", null);
  }

  private setStatus(status: SyncStatus, retryInMs: number | null): void {
    if (status === this.status && retryInMs === null) return;
    this.status = status;
    this.deps.post({ type: "status", status, retryInMs });
  }

  private flush = (): void => {
    this.flushQueued = false;
    const notice = this.coalescer.take();
    if (!notice) return;
    this.lastFlush = this.deps.now();
    this.deps.post(notice);
  };

  private offer(response: ChangesResponse): void {
    this.coalescer.add(response);
    if (!this.coalescer.pending || this.flushQueued) return;
    const wait = this.lastFlush + (this.deps.coalesceMs ?? COALESCE_MS) - this.deps.now();
    if (wait <= 0) return this.flush();
    this.flushQueued = true;
    this.deps.schedule(this.flush, wait);
  }

  /** One request. Returns false when the loop should stop (aborted). */
  async step(signal: AbortSignal): Promise<boolean> {
    const baseline = !this.primed;
    const wait = baseline ? 0 : LONG_POLL_SECONDS;
    const timeout = new AbortController();
    const onStop = () => timeout.abort();
    signal.addEventListener("abort", onStop);
    const timer = setTimeout(() => timeout.abort(), wait * 1000 + REQUEST_GRACE_MS);
    let response: Response;
    try {
      response = await this.deps.fetch(changesUrl(this.baseUrl, this.cursor, wait), {
        redirect: "error",
        headers: {
          ...(this.token ? { Authorization: `Bearer ${this.token}` } : {}),
          ...(this.browserSession ? { "X-Metis-Session": this.browserSession } : {}),
        },
        signal: timeout.signal,
      });
    } catch {
      if (signal.aborted) return false;
      return this.fail(signal);
    } finally {
      clearTimeout(timer);
      signal.removeEventListener("abort", onStop);
    }

    if (response.status === 401) {
      // Retrying a refused bearer is pointless: wait until the page hands us a new one.
      this.setStatus("unauthorized", null);
      await new Promise<void>((resolve) => (this.authWait = resolve));
      this.authWait = null;
      return !signal.aborted;
    }
    if (!response.ok) return this.fail(signal);

    let body: ChangesResponse;
    try {
      body = (await response.json()) as ChangesResponse;
    } catch {
      return this.fail(signal);
    }
    if (signal.aborted) return false;
    this.failures = 0;
    this.setStatus("live", null);
    // A baseline only primes the cursor and the queue counts; a reset still counts.
    this.offer(baseline && !body.reset ? { cursor: body.cursor, queue: body.queue } : body);
    // A baseline that still returned old history keeps catching up at wait=0.
    if (baseline && ((body.changes?.length ?? 0) === 0 || body.cursor <= this.cursor)) this.primed = true;
    this.cursor = body.cursor;
    return true;
  }

  private async fail(signal: AbortSignal): Promise<boolean> {
    this.failures += 1;
    const delay = backoffMs(this.failures);
    this.setStatus("offline", delay);
    await this.deps.sleep(delay, signal);
    return !signal.aborted;
  }

  private async run(): Promise<void> {
    const stopper = new AbortController();
    this.stopper = stopper;
    this.setStatus("connecting", null);
    while (!stopper.signal.aborted) {
      if (!(await this.step(stopper.signal))) break;
    }
  }
}
