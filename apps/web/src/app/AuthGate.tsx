/**
 * Cloud sign-in gate. Renders nothing that talks to the API until a real
 * session exists. With no Cognito config (fixture, local, desktop) it is a
 * pass-through and no login is ever shown.
 */
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { Button } from "../components/Button";
import {
  AuthError,
  authMode,
  completeSignIn,
  loadSession,
  needsRefresh,
  refreshSession,
  saveSession,
  signOutUrl,
  startSignIn,
  type AuthConfig,
  type Session,
} from "../lib/auth";
import { setAuthToken, setCookieSession, setUnauthenticatedHandler } from "../lib/api";
import { FirebaseGate } from "./FirebaseGate";
import { isDesktop, isHostedDesktop } from "../lib/desktop";
import { resetClientData } from "../lib/reset";

type AuthValue = { signedIn: boolean; signOut: (() => void) | null };
export const AuthContext = createContext<AuthValue>({ signedIn: false, signOut: null });
export const useAuth = () => useContext(AuthContext);

// The authorization code is single use; StrictMode runs effects twice in dev.
let callback: Promise<Session | null> | null = null;

function Panel({ children }: { children: ReactNode }) {
  return (
    <main className="grid min-h-dvh place-items-center bg-canvas p-6 text-ink">
      <div className="max-w-sm text-center">
        <p className="font-serif text-xl font-semibold">Metis</p>
        {children}
      </div>
    </main>
  );
}

function CognitoGate({ config, children }: { config: AuthConfig; children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [checking, setChecking] = useState(true);
  const [message, setMessage] = useState<string | null>(null);

  const drop = useCallback((why: string | null) => {
    saveSession(null);
    setAuthToken(null);
    setSession(null);
    setMessage(why);
  }, []);

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        callback ??= completeSignIn(config, window.location.href);
        let next = await callback;
        if (next) window.history.replaceState(null, "", `${window.location.pathname.replace(/\/callback$/, "/")}#/today`);
        next ??= loadSession();
        if (next && needsRefresh(next)) next = await refreshSession(config, next);
        if (next) (saveSession(next), setAuthToken(next.accessToken)); // before children mount
        if (live) setSession(next);
      } catch (cause) {
        if (live) drop(cause instanceof AuthError ? cause.message : "Sign-in failed.");
      } finally {
        if (live) setChecking(false);
      }
    })();
    return () => {
      live = false;
    };
  }, [config, drop]);

  // Keep the bearer current, and fail closed when it cannot be renewed.
  useEffect(() => {
    if (!session) return;
    setUnauthenticatedHandler(() => drop("Your session ended. Sign in again."));
    const timer = window.setTimeout(
      () =>
        refreshSession(config, session).then(
          (next) => (saveSession(next), setAuthToken(next.accessToken), setSession(next)),
          () => drop("Your session expired. Sign in again."),
        ),
      Math.max(1000, session.expiresAt - Date.now() - 60_000),
    );
    return () => {
      window.clearTimeout(timer);
      setUnauthenticatedHandler(null);
    };
  }, [session, config, drop]);

  const signOut = useCallback(() => {
    resetClientData();
    saveSession(null);
    window.location.assign(signOutUrl(config));
  }, [config]);

  const value = useMemo(() => ({ signedIn: !!session, signOut }), [session, signOut]);

  if (checking) return <Panel><p role="status" className="mt-2 text-sm text-muted">Checking your session…</p></Panel>;
  if (!session)
    return (
      <Panel>
        {message && (
          <p role="alert" className="mt-2 text-sm text-bad">
            {message}
          </p>
        )}
        <p className="mt-2 text-sm text-muted">Sign in to open your notebook.</p>
        <Button variant="primary" className="mt-4" onClick={() => void startSignIn(config)}>
          Sign in
        </Button>
      </Panel>
    );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/**
 * Single-user deployment gate (Vercel): username and password are checked by
 * the edge proxy, which sets an HttpOnly session cookie and only then forwards
 * to the VM with the access key. The browser never holds the key or the cookie
 * value; it just learns whether it is signed in.
 */
function PasswordGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState<"checking" | "out" | "in">("checking");
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    fetch("/api/session", { cache: "no-store" }).then(
      (r) => setState(r.ok ? "in" : "out"),
      () => (setState("out"), setMessage("The sign-in service could not be reached.")),
    );
  }, []);

  // Set during render: children's effects (their first requests) run before ours.
  setCookieSession(state === "in");
  useEffect(() => {
    if (state !== "in") return;
    setUnauthenticatedHandler(() => (setState("out"), setMessage("Your session ended. Sign in again.")));
    return () => setUnauthenticatedHandler(null);
  }, [state]);

  const signOut = useCallback(() => {
    resetClientData();
    void fetch("/api/session", { method: "POST" }).finally(() => (setState("out"), setMessage(null)));
  }, []);
  const value = useMemo(() => ({ signedIn: state === "in", signOut }), [state, signOut]);

  if (state === "checking")
    return <Panel><p role="status" className="mt-2 text-sm text-muted">Checking your session…</p></Panel>;
  if (state === "out")
    return (
      <Panel>
        {message && (
          <p role="alert" className="mt-2 text-sm text-bad">
            {message}
          </p>
        )}
        <form
          className="mt-2 text-left"
          onSubmit={async (e) => {
            e.preventDefault();
            const form = new FormData(e.currentTarget);
            setBusy(true);
            try {
              const r = await fetch("/api/login", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ username: form.get("username"), password: form.get("password") }),
              });
              if (r.ok) return (setMessage(null), setState("in"));
              setMessage(
                r.status === 429
                  ? `Too many attempts. Try again in ${r.headers.get("retry-after") ?? "a few"} seconds.`
                  : r.status === 401
                    ? "That username or password was not accepted."
                    : "Sign-in is unavailable right now.",
              );
            } catch {
              setMessage("The sign-in service could not be reached.");
            } finally {
              setBusy(false);
            }
          }}
        >
          <p className="text-center text-sm text-muted">Sign in to open your notebook.</p>
          <label className="mt-3 block text-sm text-muted" htmlFor="login-user">Username</label>
          <input id="login-user" name="username" autoComplete="username" required
            className="mt-1 w-full rounded-md border border-line bg-surface px-3 py-2 text-sm" />
          <label className="mt-3 block text-sm text-muted" htmlFor="login-pass">Password</label>
          <input id="login-pass" name="password" type="password" autoComplete="current-password" required
            className="mt-1 w-full rounded-md border border-line bg-surface px-3 py-2 text-sm" />
          <Button variant="primary" className="mt-4 w-full" type="submit" disabled={busy}>
            {busy ? "Signing in…" : "Sign in"}
          </Button>
        </form>
      </Panel>
    );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function AuthGate({ children }: { children: ReactNode }) {
  const env = import.meta.env as Record<string, string | undefined>;
  const mode = authMode(env, isDesktop(), isHostedDesktop());
  if (mode.kind === "firebase") return <FirebaseGate config={mode.config}>{children}</FirebaseGate>;
  if (mode.kind === "none")
    return env.VITE_METIS_LOGIN === "password" && !isDesktop() ? (
      <PasswordGate>{children}</PasswordGate>
    ) : (
      <>{children}</>
    );
  if (mode.kind === "misconfigured")
    return (
      <Panel>
        <p role="alert" className="mt-2 text-sm text-bad">
          Sign-in is only partly configured, so Metis will not open. Missing: {mode.missing.join(", ")}.
        </p>
      </Panel>
    );
  return <CognitoGate config={mode.config}>{children}</CognitoGate>;
}
