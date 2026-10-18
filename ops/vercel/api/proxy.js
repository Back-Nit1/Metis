// Edge proxy: the browser talks to this same-origin route, and the access key
// is added here from a Vercel env var, so it never reaches the client.
export const config = { runtime: "edge" };

export default async function handler(req) {
  const { METIS_API_ORIGIN: origin, METIS_ACCESS_KEY: key } = process.env;
  if (!origin || !key) return new Response("proxy not configured", { status: 500 });
  const url = new URL(req.url);
  const path = url.searchParams.get("p") ?? "";
  url.searchParams.delete("p");
  const headers = new Headers(req.headers);
  headers.set("authorization", `Bearer ${key}`);
  headers.delete("host");
  const hasBody = req.method !== "GET" && req.method !== "HEAD";
  return fetch(`${origin}/v1/${path}${url.search}`, {
    method: req.method,
    headers,
    body: hasBody ? req.body : undefined,
    duplex: "half",
    redirect: "manual",
  });
}
