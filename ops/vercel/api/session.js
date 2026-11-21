import { clearedCookie, foreignOrigin, hasSession, json } from "./_auth.js";

export const config = { runtime: "edge" };

/** GET: is this browser signed in? POST: sign out (clears the cookie). */
export default async function handler(req) {
  if (req.method === "POST") {
    if (foreignOrigin(req)) return json(403, { code: "forbidden", message: "cross-site request refused" });
    return json(200, { signed_in: false }, { "set-cookie": clearedCookie });
  }
  return (await hasSession(req))
    ? json(200, { signed_in: true })
    : json(401, { code: "unauthenticated", message: "sign in required" });
}
