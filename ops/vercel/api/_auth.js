// Shared by the edge routes (the leading underscore keeps Vercel from serving it).
//
// Single-user username/password login in front of the proxy. The password is
// stored only as a PBKDF2 hash (ops/vercel/hash-password.mjs makes one); a
// successful login sets a signed, HttpOnly session cookie. The VM access key is
// added by the proxy only for requests that carry a valid session, and never
// reaches the browser.
//
// Env (Vercel, production): METIS_LOGIN_USER, METIS_LOGIN_PASSWORD_HASH,
// METIS_SESSION_SECRET (32+ chars). Rotating METIS_SESSION_SECRET signs every
// session out.

const enc = new TextEncoder();
export const COOKIE = "__Host-metis_session";
const SESSION_SECONDS = 12 * 60 * 60;

const b64u = (buf) =>
  btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const unb64u = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));

/** Constant-time for equal-length strings; length itself is not secret here. */
function equal(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function loginConfig() {
  const { METIS_LOGIN_USER: user, METIS_LOGIN_PASSWORD_HASH: hash, METIS_SESSION_SECRET: secret } = process.env;
  if (!user || !hash || !secret || secret.length < 32) return null;
  return { user, hash, secret };
}

async function hmac(secret, message) {
  const key = await crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, [
    "sign",
  ]);
  return b64u(await crypto.subtle.sign("HMAC", key, enc.encode(message)));
}

export async function pbkdf2(password, salt, iterations) {
  const key = await crypto.subtle.importKey("raw", enc.encode(password), "PBKDF2", false, ["deriveBits"]);
  return b64u(await crypto.subtle.deriveBits({ name: "PBKDF2", hash: "SHA-256", salt, iterations }, key, 256));
}

/** Hash format: pbkdf2_sha256$<iterations>$<salt b64url>$<hash b64url>. */
async function passwordMatches(password, stored) {
  const [scheme, iterations, salt, hash] = stored.split("$");
  if (scheme !== "pbkdf2_sha256" || !salt || !hash || !(Number(iterations) >= 100_000)) return false;
  return equal(await pbkdf2(password, unb64u(salt), Number(iterations)), hash);
}

// ----- fixed-window rate limiting ------------------------------------------ //
// ponytail: per edge isolate, so a determined attacker spread across regions
// gets a few extra tries. The real defence is a long random password; this
// just stops a single client hammering the hash.
const windows = new Map();

/** rules: [key, limit, windowSeconds][]. Returns 0 when allowed, else seconds to wait. */
export function limit(rules) {
  const now = Date.now() / 1000;
  const slots = rules.map(([key, max, seconds]) => [`${key}@${seconds}`, max, seconds, Math.floor(now / seconds)]);
  for (const [id, max, seconds, start] of slots) {
    const hit = windows.get(id);
    if (hit && hit[0] === start && hit[1] >= max) return Math.ceil((start + 1) * seconds - now);
  }
  if (windows.size > 10_000) windows.clear();
  for (const [id, , , start] of slots) {
    const hit = windows.get(id);
    windows.set(id, hit && hit[0] === start ? [start, hit[1] + 1] : [start, 1]);
  }
  return 0;
}

export const clientIp = (req) =>
  req.headers.get("x-real-ip") || (req.headers.get("x-forwarded-for") || "").split(",")[0].trim() || "unknown";

// ----- sessions ------------------------------------------------------------- //
export async function checkLogin(username, password) {
  const cfg = loginConfig();
  if (!cfg) return { ok: false, configured: false };
  // Always hash, so a wrong username costs the same time as a wrong password.
  const passOk = await passwordMatches(password, cfg.hash);
  return { ok: passOk && equal(username, cfg.user), configured: true };
}

export async function sessionCookie() {
  const { secret } = loginConfig();
  const body = `${Math.floor(Date.now() / 1000) + SESSION_SECONDS}.${b64u(crypto.getRandomValues(new Uint8Array(16)))}`;
  const value = `${body}.${await hmac(secret, body)}`;
  return `${COOKIE}=${value}; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=${SESSION_SECONDS}`;
}

export const clearedCookie = `${COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=0`;

export async function hasSession(req) {
  const cfg = loginConfig();
  if (!cfg) return false;
  const raw = (req.headers.get("cookie") || "")
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${COOKIE}=`));
  const [expires, nonce, sig] = (raw ? raw.slice(COOKIE.length + 1) : "").split(".");
  if (!expires || !nonce || !sig || !(Number(expires) > Date.now() / 1000)) return false;
  return equal(await hmac(cfg.secret, `${expires}.${nonce}`), sig);
}

/** A browser always sends Origin on cross-site POST/PUT/PATCH/DELETE; refuse foreign ones. */
export function foreignOrigin(req) {
  const origin = req.headers.get("origin");
  return origin !== null && origin !== new URL(req.url).origin;
}

export const json = (status, body, headers = {}) =>
  new Response(body === null ? null : JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store", ...headers },
  });
