import { afterEach, describe, expect, it, vi } from "vitest";
import { AuthError, authMode } from "./auth";
import { browserSession, endBrowserSession } from "./browserSession";
import { loadFirebase, refreshFirebase, resetFirebasePassword, saveFirebase, signInFirebase, signInFirebaseWithGoogle } from "./firebaseAuth";
const config = { apiKey: "public-key", projectId: "metis-test" };
afterEach(() => vi.unstubAllGlobals());

describe("hosted authentication", () => {
  it("requires per-user auth on hosted desktop and never uses the password proxy", () => {
    expect(authMode({ VITE_METIS_LOGIN: "password" }, true, true).kind).toBe("misconfigured");
    expect(authMode({ VITE_METIS_LOGIN: "firebase" }, false).kind).toBe("misconfigured");
    expect(authMode({ VITE_METIS_FIREBASE_API_KEY: config.apiKey }, true, true)).toEqual({ kind: "misconfigured", missing: ["VITE_METIS_FIREBASE_PROJECT_ID"] });
    expect(authMode({ VITE_METIS_FIREBASE_API_KEY: config.apiKey, VITE_METIS_FIREBASE_PROJECT_ID: config.projectId }, true, true)).toEqual({ kind: "firebase", config });
  });
  it("sends credentials only to Identity Platform and uses the ID token", async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify({ idToken: "id-token", refreshToken: "refresh", expiresIn: "3600" })));
    vi.stubGlobal("fetch", fetch);
    const result = await signInFirebase(config, "person@example.test", "private-password");
    expect(result.accessToken).toBe("id-token");
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=public-key");
    expect(JSON.parse(String(init.body))).toEqual({ email: "person@example.test", password: "private-password", returnSecureToken: true });
    expect(init.redirect).toBe("error");
  });
  it("rotates refresh tokens, failing closed on refusal or malformed provider data", async () => {
    const previous = { accessToken: "old", refreshToken: "r1", expiresAt: 0 };
    const fetch = vi.fn(async () => new Response(JSON.stringify({ id_token: "new", refresh_token: "r2", expires_in: "3600" })));
    vi.stubGlobal("fetch", fetch);
    expect(await refreshFirebase(config, previous)).toMatchObject({ accessToken: "new", refreshToken: "r2" });
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://securetoken.googleapis.com/v1/token?key=public-key");
    expect(String(init.body)).toBe("grant_type=refresh_token&refresh_token=r1");
    vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 400 })));
    await expect(refreshFirebase(config, previous)).rejects.toBeInstanceOf(AuthError);
    vi.stubGlobal("fetch", vi.fn(async () => new Response('{"idToken":"fake"}')));
    await expect(signInFirebase(config, "e", "p")).rejects.toBeInstanceOf(AuthError);
  });
  it("requests a password reset from Identity Platform", async () => {
    const fetch = vi.fn(async (_url: string, _init: RequestInit) => new Response("{}"));
    vi.stubGlobal("fetch", fetch);
    await resetFirebasePassword(config, "person@example.test");
    const [url, init] = fetch.mock.calls[0]!;
    expect(url).toBe("https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key=public-key");
    expect(JSON.parse(String(init.body))).toEqual({ requestType: "PASSWORD_RESET", email: "person@example.test" });
  });
  it("exchanges a Google credential through the configured Identity Platform project", async () => {
    const fetch = vi.fn(async (_url: string, _init: RequestInit) => new Response(
      JSON.stringify({ idToken: "firebase-id", refreshToken: "refresh", expiresIn: "3600" }),
    ));
    vi.stubGlobal("window", { location: { origin: "https://metis.example.test" } });
    vi.stubGlobal("fetch", fetch);
    expect((await signInFirebaseWithGoogle(config, "google-id")).accessToken).toBe("firebase-id");
    const [url, init] = fetch.mock.calls[0]!;
    expect(url).toBe("https://identitytoolkit.googleapis.com/v1/accounts:signInWithIdp?key=public-key");
    expect(JSON.parse(String(init.body))).toMatchObject({
      postBody: "id_token=google-id&providerId=google.com", requestUri: "https://metis.example.test",
    });
  });
  it("exchanges an ID token for a same-origin cookie and revokes it on sign-out", async () => {
    const binding = "a".repeat(64);
    const fetch = vi.fn(async (_url: string, _init: RequestInit) =>
      new Response(null, { status: 204, headers: { "X-Metis-Session": binding } }),
    );
    vi.stubGlobal("fetch", fetch);
    expect(await browserSession("id-token")).toBe(binding);
    expect(await browserSession()).toBe(binding);
    await endBrowserSession(binding);
    expect(fetch.mock.calls).toEqual([
      ["/v1/session", expect.objectContaining({ method: "POST", credentials: "same-origin", headers: { Authorization: "Bearer id-token" } })],
      ["/v1/session", expect.objectContaining({ method: "GET", headers: undefined })],
      ["/v1/session", expect.objectContaining({ method: "DELETE", headers: { "X-Metis-Session": binding } })],
    ]);
    const env = { VITE_METIS_LOGIN: "firebase-cookie", VITE_METIS_API: "", VITE_METIS_FIREBASE_API_KEY: config.apiKey, VITE_METIS_FIREBASE_PROJECT_ID: config.projectId };
    expect(authMode(env, false).kind).toBe("firebase-cookie");
    expect(authMode({ ...env, VITE_METIS_API: "https://elsewhere.test" }, false).kind).toBe("misconfigured");
    expect(authMode(env, true, true).kind).toBe("firebase");
  });
  it("isolates remembered sessions by project and clears them on logout", () => {
    const data = new Map<string, string>();
    const storage = { getItem: (k: string) => data.get(k) ?? null, setItem: (k: string, v: string) => void data.set(k, v), removeItem: (k: string) => void data.delete(k) } as Storage;
    const session = { accessToken: "id", refreshToken: "r", expiresAt: Date.now() + 1000 };
    saveFirebase(config, session, storage);
    expect(loadFirebase(config, storage)).toEqual(session);
    expect(loadFirebase({ ...config, projectId: "another" }, storage)).toBeNull();
    saveFirebase(config, null, storage);
    expect(loadFirebase(config, storage)).toBeNull();
  });
});
