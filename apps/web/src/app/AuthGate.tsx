/**
 * Sign-in gate. Renders nothing that talks to the API until a real session
 * exists. On the local-development desktop (loopback sidecars) it is a
 * pass-through and no login is ever shown.
 */
import { createContext, useContext, type ReactNode } from "react";
import { authMode } from "../lib/auth";
import { FirebaseGate } from "./FirebaseGate";
import { isHostedDesktop } from "../lib/desktop";

type AuthValue = { signedIn: boolean; signOut: (() => void) | null };
export const AuthContext = createContext<AuthValue>({ signedIn: false, signOut: null });
export const useAuth = () => useContext(AuthContext);

export function AuthGate({ children }: { children: ReactNode }) {
  const mode = authMode(import.meta.env as Record<string, string | undefined>, isHostedDesktop());
  if (mode.kind === "firebase") return <FirebaseGate config={mode.config}>{children}</FirebaseGate>;
  if (mode.kind === "none") return <>{children}</>;
  return (
    <main className="grid min-h-dvh place-items-center bg-canvas p-6 text-ink">
      <div className="max-w-sm text-center">
        <p className="font-serif text-xl font-semibold">Metis</p>
        <p role="alert" className="mt-2 text-sm text-bad">
          Sign-in is only partly configured, so Metis will not open. Missing: {mode.missing.join(", ")}.
        </p>
      </div>
    </main>
  );
}
