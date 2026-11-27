/** Desktop connection supplied by Rust. Hosted is an HTTPS API with a per-user
 * session; explicit local development uses supervised loopback services.
 * Connection data stays in memory. Hosted startup does not require a backend.
 */

export type DesktopPhase = "browser" | "starting" | "ready" | "unavailable";
export type DesktopState = { phase: DesktopPhase; reason: string | null };
export type Invoke = (command: string) => Promise<unknown>;

type Connection = { base: string; bearer: string; mode: "local" | "hosted" };

const LOOPBACK = /^http:\/\/127\.0\.0\.1:(\d{1,5})\/?$/;
// Header-safe: a bearer with whitespace or control characters is malformed.
const BEARER = /^[\x21-\x7e]{1,4096}$/;

let connection: Connection | null = null;
let state: DesktopState = { phase: isDesktop() ? "starting" : "browser", reason: null };
const listeners = new Set<() => void>();

export function isDesktop(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

function setState(next: DesktopState): void {
  state = next;
  listeners.forEach((l) => l());
}

export const subscribeDesktop = (l: () => void) => (listeners.add(l), () => void listeners.delete(l));
export const getDesktopState = (): DesktopState => state;

export const isHostedDesktop = (): boolean => connection?.mode === "hosted";

/** Configured API base while connected; never falls back to another server. */
export function desktopBase(): string | null {
  return connection?.base ?? null;
}

/** Authorization only for the configured API origin. */
export function desktopAuthFor(url: string): Record<string, string> {
  if (!connection || !connection.bearer) return {};
  try {
    return new URL(url).origin === new URL(connection.base).origin
      ? { Authorization: `Bearer ${connection.bearer}` }
      : {};
  } catch {
    return {};
  }
}

/** The current bearer, passed to the sync worker in memory. */
export function desktopBearer(): string | null {
  return connection?.bearer ?? null;
}

/**
 * Calls an optional desktop command. Resolves `{ ok: false }` in the browser or
 * when the shell does not implement it, so callers can feature-detect.
 */
export async function desktopCommand<T>(
  command: string,
  args?: Record<string, unknown>,
): Promise<{ ok: true; value: T } | { ok: false; error?: string }> {
  if (!isDesktop()) return { ok: false };
  try {
    const invoke = (window as unknown as { __TAURI_INTERNALS__: { invoke: (c: string, a?: unknown) => Promise<unknown> } })
      .__TAURI_INTERNALS__.invoke;
    return { ok: true, value: (await invoke(command, args)) as T };
  } catch (cause) {
    // Rust commands reject with their message string.
    return { ok: false, error: typeof cause === "string" ? cause.slice(0, 300) : undefined };
  }
}

export function parseConnection(raw: unknown): Connection | null {
  if (typeof raw !== "object" || raw === null) return null;
  const { apiBaseUrl, bearer, mode } = raw as Record<string, unknown>;
  if (typeof apiBaseUrl !== "string" || typeof bearer !== "string") return null;
  if (mode === "hosted") {
    try {
      const url = new URL(apiBaseUrl);
      if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash || url.pathname !== "/" || bearer !== "") return null;
      return { base: url.origin, bearer: "", mode: "hosted" };
    } catch { return null; }
  }
  if (mode !== undefined && mode !== "local") return null;
  const match = LOOPBACK.exec(apiBaseUrl);
  if (!match || Number(match[1]) < 1 || Number(match[1]) > 65535) return null;
  if (!BEARER.test(bearer)) return null;
  return { base: apiBaseUrl.replace(/\/$/, ""), bearer, mode: "local" };
}

const tauriInvoke: Invoke = (command) =>
  (
    window as unknown as { __TAURI_INTERNALS__: { invoke: Invoke } }
  ).__TAURI_INTERNALS__.invoke(command);

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** Waits for the sidecars with bounded backoff. Safe to call again to retry. */
export async function connectDesktop(
  options: { invoke?: Invoke; sleep?: (ms: number) => Promise<void>; maxAttempts?: number } = {},
): Promise<DesktopState> {
  if (!isDesktop() && !options.invoke) return state;
  const invoke = options.invoke ?? tauriInvoke;
  const wait = options.sleep ?? sleep;
  const maxAttempts = options.maxAttempts ?? 40;
  connection = null;
  setState({ phase: "starting", reason: null });
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    try {
      const parsed = parseConnection(await invoke("desktop_connection"));
      if (!parsed) {
        setState({ phase: "unavailable", reason: "The desktop runtime returned invalid connection data." });
        return state;
      }
      connection = parsed;
      setState({ phase: "ready", reason: null });
      return state;
    } catch (error) {
      if (error !== "not_ready") {
        // The raw error is deliberately not shown: it is not ours to trust.
        setState({ phase: "unavailable", reason: "The desktop server connection is unavailable. Check the server configuration." });
        return state;
      }
    }
    await wait(Math.min(250 * 1.5 ** attempt, 2000));
  }
  setState({ phase: "unavailable", reason: "The desktop connection did not become ready in time." });
  return state;
}

/** Forget the runtime connection (sign-out / profile reset). */
export function clearDesktopConnection(): void {
  connection = null;
  if (isDesktop()) setState({ phase: "starting", reason: null });
}

/** Test helper. */
export function resetDesktopForTests(): void {
  connection = null;
  state = { phase: isDesktop() ? "starting" : "browser", reason: null };
}

/** Native capture uses the same user ID token as the webview. Fail closed if IPC fails. */
let sessionWrite: Promise<void> = Promise.resolve();
export async function setDesktopSession(token: string | null): Promise<void> {
  if (!isHostedDesktop()) return;
  if (token !== null && !BEARER.test(token)) throw new Error("Invalid session token.");
  const write = sessionWrite.catch(() => undefined).then(async () => {
    const result = await desktopCommand("desktop_set_session", { token });
    if (!result.ok) throw new Error("The desktop session could not be updated.");
    if (connection?.mode === "hosted") connection.bearer = token ?? "";
  });
  sessionWrite = write;
  await write;
}
