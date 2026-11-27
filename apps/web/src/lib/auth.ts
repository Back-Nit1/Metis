/**
 * Sign-in mode. Local development (loopback sidecars) has no login at all.
 * The hosted desktop requires a Firebase / Identity Platform session and never
 * falls back to the fixture identity. Configuration is two public Vite
 * variables; no secret exists in the bundle.
 */
export type FirebaseConfig = { apiKey: string; projectId: string };

export type AuthMode =
  | { kind: "firebase"; config: FirebaseConfig }
  | { kind: "none" }
  | { kind: "misconfigured"; missing: string[] };

export type Session = { accessToken: string; refreshToken: string | null; expiresAt: number };

type Env = Record<string, string | undefined>;
const FIREBASE_KEYS = ["VITE_METIS_FIREBASE_API_KEY", "VITE_METIS_FIREBASE_PROJECT_ID"] as const;

export function authMode(env: Env, hosted: boolean): AuthMode {
  if (!hosted) return { kind: "none" };
  const missing = FIREBASE_KEYS.filter((k) => !env[k]?.trim());
  return missing.length ? { kind: "misconfigured", missing } : {
    kind: "firebase",
    config: { apiKey: env.VITE_METIS_FIREBASE_API_KEY!.trim(), projectId: env.VITE_METIS_FIREBASE_PROJECT_ID!.trim() },
  };
}

export class AuthError extends Error {}
