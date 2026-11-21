import { checkLogin, clientIp, foreignOrigin, json, limit, sessionCookie } from "./_auth.js";

export const config = { runtime: "edge" };

export default async function handler(req) {
  if (req.method !== "POST") return json(405, { code: "validation_failed", message: "use POST" });
  if (foreignOrigin(req)) return json(403, { code: "forbidden", message: "cross-site login refused" });

  const ip = clientIp(req);
  const wait = limit([
    [`login:${ip}`, 5, 60],
    [`login:${ip}`, 20, 3600],
    ["login:all", 30, 60],
  ]);
  if (wait)
    return json(429, { code: "rate_limited", message: "too many sign-in attempts" }, { "retry-after": String(wait) });

  let body = {};
  try {
    body = await req.json();
  } catch {
    /* treated as empty credentials */
  }
  const result = await checkLogin(String(body?.username ?? ""), String(body?.password ?? ""));
  if (!result.configured) return json(500, { code: "internal", message: "sign-in is not configured" });
  if (!result.ok) return json(401, { code: "unauthenticated", message: "wrong username or password" });
  return json(200, { signed_in: true }, { "set-cookie": await sessionCookie() });
}
