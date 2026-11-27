/** Google Identity Platform REST authentication; public API key, never a server secret.
 * https://firebase.google.com/docs/reference/rest/auth
 * ID tokens are verified by Metis; decoding claims here would not authenticate a user.
 */
import { AuthError, type FirebaseConfig, type Session } from "./auth";

async function exchange(url: string, init: RequestInit): Promise<Record<string, unknown>> {
  let response: Response;
  try { response = await fetch(url, { ...init, redirect: "error" }); }
  catch { throw new AuthError("The sign-in service could not be reached."); }
  if (!response.ok) throw new AuthError(response.status === 429 ? "Too many sign-in attempts. Try again later." : "Sign-in was refused or your session expired.");
  try { return await response.json() as Record<string, unknown>; }
  catch { throw new AuthError("The sign-in service returned an unusable response."); }
}

function session(token: unknown, refresh: unknown, seconds: unknown): Session {
  const ttl = Number(seconds);
  if (typeof token !== "string" || !token || typeof refresh !== "string" || !refresh || !Number.isFinite(ttl) || ttl <= 0) {
    throw new AuthError("The sign-in service returned an unusable response.");
  }
  return { accessToken: token, refreshToken: refresh, expiresAt: Date.now() + ttl * 1000 };
}

export async function signInFirebase(config: FirebaseConfig, email: string, password: string): Promise<Session> {
  const data = await exchange(`https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=${encodeURIComponent(config.apiKey)}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password, returnSecureToken: true }),
  });
  return session(data.idToken, data.refreshToken, data.expiresIn);
}

export async function refreshFirebase(config: FirebaseConfig, previous: Session): Promise<Session> {
  if (!previous.refreshToken) throw new AuthError("Your session expired. Sign in again.");
  const data = await exchange(`https://securetoken.googleapis.com/v1/token?key=${encodeURIComponent(config.apiKey)}`, {
    method: "POST", headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ grant_type: "refresh_token", refresh_token: previous.refreshToken }),
  });
  return session(data.id_token, data.refresh_token, data.expires_in);
}

export async function resetFirebasePassword(config: FirebaseConfig, email: string): Promise<void> {
  await exchange(`https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key=${encodeURIComponent(config.apiKey)}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ requestType: "PASSWORD_RESET", email }),
  });
}

const key = (config: FirebaseConfig) => `metis.firebase.${config.projectId}`;
export function loadFirebase(config: FirebaseConfig, storage: Storage = sessionStorage): Session | null {
  try {
    const value = JSON.parse(storage.getItem(key(config)) ?? "null") as Session | null;
    return value && typeof value.accessToken === "string" && typeof value.refreshToken === "string" && Number.isFinite(value.expiresAt) ? value : null;
  } catch { return null; }
}
export function saveFirebase(config: FirebaseConfig, value: Session | null, storage: Storage = sessionStorage): void {
  try {
    if (value) storage.setItem(key(config), JSON.stringify(value));
    else storage.removeItem(key(config));
  } catch { /* Session remains in memory when storage is unavailable. */ }
}
