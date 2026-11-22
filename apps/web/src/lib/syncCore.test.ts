import { describe, expect, it } from "vitest";
import type { ChangesResponse } from "../contracts/metis";
import { BACKOFF_CAP_MS, Coalescer, SyncLoop, backoffMs, changesUrl, type WorkerOut } from "./syncCore";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });

function harness(responses: (Response | Error)[]) {
  const urls: string[] = [];
  const auth: (string | undefined)[] = [];
  const out: WorkerOut[] = [];
  const sleeps: number[] = [];
  const scheduled: { fn: () => void; ms: number }[] = [];
  let clock = 0;
  const loop = new SyncLoop({
    fetch: async (url, init) => {
      urls.push(url);
      auth.push((init.headers as Record<string, string>).Authorization);
      const next = responses.shift();
      if (!next) throw new Error("no more responses");
      if (next instanceof Error) throw next;
      return next;
    },
    post: (m) => out.push(m),
    now: () => clock,
    sleep: async (ms) => void sleeps.push(ms),
    schedule: (fn, ms) => void scheduled.push({ fn, ms }),
  });
  return {
    loop,
    urls,
    auth,
    out,
    sleeps,
    scheduled,
    tick: (ms: number) => (clock += ms),
    changes: () => out.filter((m) => m.type === "changes"),
    statuses: () => out.filter((m) => m.type === "status").map((m) => m.type === "status" && m.status),
  };
}

const signal = () => new AbortController().signal;
const resp = (r: ChangesResponse) => json(r);

describe("backoff", () => {
  it("doubles from one second and caps at thirty", () => {
    expect([1, 2, 3, 4, 5, 6, 7, 20].map((n) => backoffMs(n))).toEqual([1000, 2000, 4000, 8000, 16000, 30000, 30000, BACKOFF_CAP_MS]);
  });
});

describe("changesUrl", () => {
  it("strips a trailing slash and carries cursor and wait", () => {
    expect(changesUrl("http://127.0.0.1:9/", 42, 25)).toBe("http://127.0.0.1:9/v1/changes?after=42&wait=25");
  });
});

describe("Coalescer", () => {
  it("merges kinds and ids, keeps the latest queue, and clears on take", () => {
    const c = new Coalescer();
    c.add({ cursor: 1, changes: [{ seq: 1, kind: "task", id: "t1" }] });
    c.add({ cursor: 3, changes: [{ seq: 2, kind: "task", id: "t1" }, { seq: 3, kind: "source", id: "s1" }], queue: { lanes: [] } });
    const n = c.take();
    expect(n?.kinds.sort()).toEqual(["source", "task"]);
    expect(n?.ids.sort()).toEqual(["s1", "t1"]);
    expect(n?.queue).toEqual({ lanes: [] });
    expect(n?.reset).toBe(false);
    expect(c.take()).toBeNull();
  });

  it("an unchanged queue alone is not news; a changed one is", () => {
    const c = new Coalescer();
    const q = { lanes: [{ lane: "ingest" as const, pending: 1 }] };
    c.add({ cursor: 1, queue: q });
    expect(c.take()).not.toBeNull();
    c.add({ cursor: 1, queue: q });
    expect(c.take()).toBeNull();
    c.add({ cursor: 1, queue: { lanes: [{ lane: "ingest", pending: 0 }] } });
    expect(c.take()?.queue?.lanes?.[0]?.pending).toBe(0);
  });

  it("carries a reset even with no changes", () => {
    const c = new Coalescer();
    c.add({ cursor: 0, reset: true });
    expect(c.take()).toMatchObject({ reset: true, kinds: [] });
  });
});

describe("SyncLoop", () => {
  it("skips history with a wait=0 baseline, then long-polls from the cursor", async () => {
    const s = signal();
    const g = harness([
      resp({ cursor: 7, changes: [{ seq: 7, kind: "task", id: "old" }] }),
      resp({ cursor: 7, changes: [] }),
      resp({ cursor: 8, changes: [{ seq: 8, kind: "task", id: "new" }] }),
    ]);
    await g.loop.step(s);
    await g.loop.step(s);
    expect(g.loop.primed).toBe(true);
    expect(g.changes()).toHaveLength(0);
    await g.loop.step(s);
    expect(g.urls).toEqual([
      "/v1/changes?after=0&wait=0",
      "/v1/changes?after=7&wait=0",
      "/v1/changes?after=7&wait=25",
    ]);
    expect(g.loop.cursor).toBe(8);
    expect(g.changes()).toEqual([{ type: "changes", kinds: ["task"], ids: ["new"], queue: null, reset: false }]);
  });

  it("passes a reset through and adopts the server cursor", async () => {
    const h = harness([resp({ cursor: 3, changes: [] }), resp({ cursor: 1, reset: true })]);
    const s = signal();
    await h.loop.step(s);
    await h.loop.step(s);
    expect(h.changes()[0]).toMatchObject({ reset: true });
    expect(h.loop.cursor).toBe(1);
  });

  it("backs off with an honest offline status and recovers", async () => {
    const h = harness([new TypeError("down"), json({}, 503), resp({ cursor: 2, changes: [] })]);
    const s = signal();
    await h.loop.step(s);
    await h.loop.step(s);
    expect(h.sleeps).toEqual([1000, 2000]);
    expect(h.out.filter((m) => m.type === "status")).toEqual([
      { type: "status", status: "offline", retryInMs: 1000 },
      { type: "status", status: "offline", retryInMs: 2000 },
    ]);
    await h.loop.step(s);
    expect(h.statuses().at(-1)).toBe("live");
    expect(h.loop.cursor).toBe(2);
  });

  it("coalesces bursts into one notice per window", async () => {
    const h = harness([
      resp({ cursor: 0, changes: [] }),
      resp({ cursor: 1, changes: [{ seq: 1, kind: "task", id: "a" }] }),
      resp({ cursor: 2, changes: [{ seq: 2, kind: "proposal", id: "b" }] }),
      resp({ cursor: 3, changes: [{ seq: 3, kind: "task", id: "c" }] }),
    ]);
    const s = signal();
    await h.loop.step(s); // baseline
    await h.loop.step(s); // first notice goes out immediately
    expect(h.changes()).toHaveLength(1);
    h.tick(50);
    await h.loop.step(s);
    h.tick(50);
    await h.loop.step(s);
    expect(h.changes()).toHaveLength(1);
    expect(h.scheduled).toHaveLength(1);
    expect(h.scheduled[0]?.ms).toBe(250);
    h.scheduled[0]?.fn();
    expect(h.changes()[1]).toMatchObject({ kinds: ["proposal", "task"], ids: ["b", "c"] });
  });

  it("waits for a new token after a 401 instead of retrying", async () => {
    const h = harness([json({}, 401), resp({ cursor: 0, changes: [] })]);
    const s = signal();
    const pending = h.loop.step(s);
    await new Promise((r) => setTimeout(r, 0));
    expect(h.statuses()).toEqual(["unauthorized"]);
    h.loop.handle({ type: "token", token: "fresh" });
    expect(await pending).toBe(true);
    await h.loop.step(s);
    expect(h.auth[1]).toBe("Bearer fresh");
  });
});
