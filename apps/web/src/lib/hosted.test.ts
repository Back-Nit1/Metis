import { afterEach, describe, expect, it, vi } from "vitest";
import { api, setAuthToken, syncCredentials } from "./api";
import { connectDesktop, isHostedDesktop, parseConnection, resetDesktopForTests, setDesktopSession } from "./desktop";
import { Outbox } from "./outbox";
import { parseGate } from "./recordingGate";
const base = "https://metis.example.test";
afterEach(() => { setAuthToken(null); resetDesktopForTests(); vi.unstubAllGlobals(); });

describe("hosted desktop", () => {
  it("uploads capture bytes to the server under the current user, then reads the transcript there", async () => {
    const invoke = vi.fn(async (command: string) => command === "desktop_connection" ? { apiBaseUrl: base, bearer: "", mode: "hosted" } : null);
    vi.stubGlobal("window", { __TAURI_INTERNALS__: { invoke } });
    await connectDesktop({ invoke });
    expect(isHostedDesktop()).toBe(true);
    await setDesktopSession("user-id-token");
    setAuthToken("user-id-token");
    const calls: { url: string; init: RequestInit }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url, init });
      return new Response(JSON.stringify(url.endsWith("/v1/captures") ? { capture_id: "src_1", revision: 1, upload: { url: "/v1/local-uploads/src_1", object_key: "k", expires_at: "", max_bytes: 100 } } : url.endsWith("/complete") ? { capture_id: "src_1" } : { transcript_text: "server transcript" }));
    }));
    const capture = await api.createCapture({ kind: "audio" as never, capture_timezone: "UTC", captured_at: "2026-11-26T12:00:00Z", content_type: "audio/wav" }, "capture:one");
    await api.uploadBytes(capture.upload!, new Blob(["audio"], { type: "audio/wav" }));
    await api.completeCapture(capture.capture_id, { expected_revision: 1, actual_bytes: 5 }, "complete:one");
    expect((await api.getCapture(capture.capture_id)).transcript_text).toBe("server transcript");
    expect(calls.map((c) => c.url)).toEqual([`${base}/v1/captures`, `${base}/v1/local-uploads/src_1`, `${base}/v1/captures/src_1/complete`, `${base}/v1/captures/src_1`]);
    for (const call of calls) {
      expect((call.init.headers as Record<string, string>).Authorization).toBe("Bearer user-id-token");
      expect(call.init.redirect).toBe("error");
      expect(String(call.init.body)).not.toContain("user_id");
    }
    expect(syncCredentials()).toEqual({ baseUrl: base, token: "user-id-token" });
    expect(invoke).toHaveBeenCalledWith("desktop_set_session", { token: "user-id-token" });
    await setDesktopSession(null);
    setAuthToken(null);
    expect(syncCredentials()?.token).toBeNull();
  });
  it("rejects insecure endpoints, credentials, paths and bundled server secrets", () => {
    for (const url of ["http://metis.example.test", `${base}/v1`, `${base}?key=secret`, "https://user:password@metis.example.test", `${base}#fragment`]) {
      expect(parseConnection({ apiBaseUrl: url, mode: "hosted", bearer: "" })).toBeNull();
    }
    expect(parseConnection({ apiBaseUrl: base, mode: "hosted", bearer: "shared-secret" })).toBeNull();
  });
  it("does not forward the user's token to a lookalike upload origin", async () => {
    vi.stubGlobal("window", { __TAURI_INTERNALS__: {} });
    await connectDesktop({ invoke: async () => ({ apiBaseUrl: base, bearer: "", mode: "hosted" }) });
    setAuthToken("private-token");
    const fetch = vi.fn(async () => new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetch);
    await api.uploadBytes({ url: `${base}.attacker.test/audio`, object_key: "k", expires_at: "", max_bytes: 10 }, new Blob(["a"]));
    expect(JSON.stringify(fetch.mock.calls)).not.toContain("private-token");
  });
  it("keeps offline hosted drafts in screen memory, not device-wide storage", async () => {
    vi.stubGlobal("window", { __TAURI_INTERNALS__: {} });
    await connectDesktop({ invoke: async () => ({ apiBaseUrl: base, bearer: "", mode: "hosted" }) });
    const setItem = vi.fn();
    vi.stubGlobal("localStorage", { setItem, removeItem: vi.fn(), getItem: vi.fn() });
    const first = new Outbox();
    first.add({ client_capture_id: "draft", text: "private", captured_at: "", capture_timezone: "UTC" });
    expect(first.list()).toHaveLength(1);
    expect(new Outbox().list()).toEqual([]);
    expect(setItem).not.toHaveBeenCalled();
  });
  it("gates recording on hosted authentication, without waiting for local services", () => {
    expect(parseGate({ mode: "hosted", authenticated: true, recording_allowed: true, stack: null })).toEqual({ allowed: true, reason: null });
    expect(parseGate({ mode: "hosted", authenticated: false, recording_allowed: true }).allowed).toBe(false);
  });
});
