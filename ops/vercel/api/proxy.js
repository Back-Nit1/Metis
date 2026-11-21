// Edge proxy: the browser talks to this same-origin route. Only a request with
// a valid login session (see _auth.js) is forwarded, and only then is the VM
// access key added from a Vercel env var. The key never reaches the client.
import { foreignOrigin, hasSession, json } from "./_auth.js";

export const config = { runtime: "edge" };

const unsafePath = (path) =>
  path.split("/").some((segment) => {
    try {
      const s = decodeURIComponent(segment);
      return s === "." || s === ".." || s.includes("/") || s.includes("\\");
    } catch {
      return true;
    }
  });

export default async function handler(req) {
  const { METIS_API_ORIGIN: origin, METIS_ACCESS_KEY: key } = process.env;
  if (!origin || !key) return json(500, { code: "internal", message: "proxy not configured" });
  if (!(await hasSession(req))) return json(401, { code: "unauthenticated", message: "sign in required" });
  if (foreignOrigin(req)) return json(403, { code: "forbidden", message: "cross-site request refused" });

  const url = new URL(req.url);
  const path = url.searchParams.get("p") ?? "";
  if (unsafePath(path)) return json(400, { code: "validation_failed", message: "invalid path" });
  url.searchParams.delete("p");

  const headers = new Headers(req.headers);
  headers.delete("cookie"); // the session cookie is for this proxy, not the VM
  headers.delete("host");
  headers.set("authorization", `Bearer ${key}`);
  const hasBody = req.method !== "GET" && req.method !== "HEAD";
  return fetch(`${origin}/v1/${path}${url.search}`, {
    method: req.method,
    headers,
    body: hasBody ? req.body : undefined,
    duplex: "half",
    redirect: "manual",
  });
}
