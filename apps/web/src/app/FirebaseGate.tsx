import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Button } from "../components/Button";
import { api, setAuthToken, setCookieSession, setUnauthenticatedHandler } from "../lib/api";
import { AuthError, type FirebaseConfig, type Session } from "../lib/auth";
import { setDesktopSession } from "../lib/desktop";
import { loadFirebase, refreshFirebase, resetFirebasePassword, saveFirebase, signInFirebase } from "../lib/firebaseAuth";
import { Outbox } from "../lib/outbox";
import { AuthContext } from "./AuthGate";

export function FirebaseGate({ config, children }: { config: FirebaseConfig; children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [checking, setChecking] = useState(true);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [resetSent, setResetSent] = useState(false);
  const generation = useRef(0);
  // Stable primitive dependencies keep config object creation from restarting auth.
  const { apiKey, projectId } = config;
  const drop = useCallback((why: string | null) => {
    generation.current++;
    setAuthToken(null);
    setSession(null);
    saveFirebase({ apiKey, projectId }, null);
    new Outbox().clear();
    setMessage(why);
    void setDesktopSession(null).catch(() => setMessage("Sign-out could not reach the desktop. Close Metis to end native capture."));
  }, [apiKey, projectId]);

  const accept = useCallback(async (next: Session, epoch: number) => {
    if (epoch !== generation.current) return;
    setAuthToken(next.accessToken);
    // Ask the authoritative server to verify this token before mounting any data UI.
    try {
      await api.preferences();
      if (epoch !== generation.current) return;
      await setDesktopSession(next.accessToken);
      if (epoch !== generation.current) return;
      saveFirebase({ apiKey, projectId }, next);
      setSession(next);
    } catch (error) {
      if (epoch === generation.current) { setAuthToken(null); throw error; }
    }
  }, [apiKey, projectId]);

  useEffect(() => {
    const epoch = ++generation.current;
    let live = true;
    setCookieSession(false);
    setAuthToken(null);
    new Outbox().clear();
    void (async () => {
      try {
        await setDesktopSession(null);
        const saved = loadFirebase({ apiKey, projectId });
        if (saved) await accept(await refreshFirebase({ apiKey, projectId }, saved), epoch);
      } catch { if (epoch === generation.current) drop("Your session ended. Sign in again."); }
      finally { if (live) setChecking(false); }
    })();
    return () => { live = false; generation.current++; };
  }, [apiKey, projectId, accept, drop]);

  useEffect(() => {
    if (!session) return;
    setUnauthenticatedHandler(() => drop("Your session ended. Sign in again."));
    const timer = window.setTimeout(() => {
      const epoch = generation.current;
      void refreshFirebase({ apiKey, projectId }, session).then((next) => accept(next, epoch)).catch(() => {
        if (epoch === generation.current) drop("Your session expired. Sign in again.");
      });
    }, Math.max(1000, session.expiresAt - Date.now() - 60_000));
    return () => { window.clearTimeout(timer); setUnauthenticatedHandler(null); };
  }, [session, apiKey, projectId, accept, drop]);

  if (session) return <AuthContext.Provider value={{ signedIn: true, signOut: () => drop(null) }}>{children}</AuthContext.Provider>;
  return <main className="grid min-h-dvh place-items-center bg-canvas p-6 text-ink">
    <div className="w-full max-w-sm">
      <h1 className="text-center font-serif text-xl font-semibold">Metis</h1>
      {checking ? <p role="status" className="mt-3 text-center text-sm text-muted">Checking your session…</p> : <form className="mt-3" onSubmit={async (event) => {
        event.preventDefault();
        const fields = new FormData(event.currentTarget);
        const epoch = ++generation.current;
        setBusy(true); setMessage(null); setResetSent(false);
        try {
          await accept(await signInFirebase({ apiKey, projectId }, String(fields.get("email")), String(fields.get("password"))), epoch);
        } catch (error) {
          if (epoch === generation.current) drop(error instanceof AuthError ? error.message : "The Metis server could not verify your session.");
        } finally { setBusy(false); }
      }}>
        <p className="text-center text-sm text-muted">Sign in to your Metis account. Your notes and recordings are stored on the server.</p>
        <label className="mt-4 block text-sm" htmlFor="firebase-email">Email</label>
        <input className="mt-1 w-full rounded-md border border-line bg-surface px-3 py-2" id="firebase-email" name="email" type="email" autoComplete="username" required />
        <label className="mt-3 block text-sm" htmlFor="firebase-password">Password</label>
        <input className="mt-1 w-full rounded-md border border-line bg-surface px-3 py-2" id="firebase-password" name="password" type="password" autoComplete="current-password" required />
        <Button className="mt-4 w-full" variant="primary" type="submit" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</Button>
        <button className="mt-3 w-full text-sm underline" type="button" disabled={busy} onClick={async (event) => {
          const email = event.currentTarget.form?.elements.namedItem("email");
          if (!(email instanceof HTMLInputElement) || !email.reportValidity()) return;
          setBusy(true); setMessage(null); setResetSent(false);
          try {
            await resetFirebasePassword({ apiKey, projectId }, email.value.trim());
            setResetSent(true);
          } catch {
            setMessage("Password reset is temporarily unavailable.");
          } finally { setBusy(false); }
        }}>Set or reset password</button>
      </form>}
      {resetSent && <p role="status" className="mt-3 text-sm text-muted">If a Metis account exists for that email, you’ll receive a password reset link.</p>}
      {message && <p role="alert" className="mt-3 text-sm text-bad">{message}</p>}
    </div>
  </main>;
}
