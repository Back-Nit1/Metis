// node --test ops/vercel/proxy.test.mjs
// Pins that the proxy never forwards (or attaches the VM key to) a request
// without a valid signed session.
import assert from "node:assert/strict";
import { test } from "node:test";
import { limit, pbkdf2 } from "./api/_auth.js";

const salt = new Uint8Array(16);
process.env.METIS_API_ORIGIN = "https://vm.example";
process.env.METIS_ACCESS_KEY = "VM_KEY_SECRET";
process.env.METIS_LOGIN_USER = "owner";
process.env.METIS_LOGIN_PASSWORD_HASH = `pbkdf2_sha256$100000$${Buffer.from(salt).toString("base64url")}$${await pbkdf2("correct horse battery", salt, 100000)}`;
process.env.METIS_SESSION_SECRET = "s".repeat(40);

const { default: proxy } = await import("./api/proxy.js");
const { default: login } = await import("./api/login.js");

const forwarded = [];
globalThis.fetch = async (url, init) => (forwarded.push({ url, init }), new Response("{}", { status: 200 }));
const ORIGIN = "https://metis.example";
const call = (handler, path, init = {}) => handler(new Request(`${ORIGIN}${path}`, init));

test("no session: 401 and nothing is forwarded", async () => {
  forwarded.length = 0;
  const r = await call(proxy, "/api/proxy?p=captures");
  assert.equal(r.status, 401);
  assert.equal(forwarded.length, 0);
});

test("a client-supplied bearer does not get through", async () => {
  forwarded.length = 0;
  const r = await call(proxy, "/api/proxy?p=captures", { headers: { authorization: "Bearer VM_KEY_SECRET" } });
  assert.equal(r.status, 401);
  assert.equal(forwarded.length, 0);
});

test("forged, tampered and expired cookies are refused", async () => {
  forwarded.length = 0;
  for (const value of ["x", "9999999999.abc.def", `${Math.floor(Date.now() / 1000) - 1}.abc.def`]) {
    const r = await call(proxy, "/api/proxy?p=captures", { headers: { cookie: `__Host-metis_session=${value}` } });
    assert.equal(r.status, 401);
  }
  assert.equal(forwarded.length, 0);
});

test("wrong password: 401, no cookie", async () => {
  const r = await call(login, "/api/login", {
    method: "POST",
    headers: { "content-type": "application/json", "x-real-ip": "1.1.1.1" },
    body: JSON.stringify({ username: "owner", password: "wrong" }),
  });
  assert.equal(r.status, 401);
  assert.equal(r.headers.get("set-cookie"), null);
});

test("login attempts are limited per client", async () => {
  const attempt = () =>
    call(login, "/api/login", {
      method: "POST",
      headers: { "content-type": "application/json", "x-real-ip": "2.2.2.2" },
      body: JSON.stringify({ username: "owner", password: "wrong" }),
    });
  const codes = [];
  for (let i = 0; i < 6; i++) codes.push((await attempt()).status);
  assert.deepEqual(codes, [401, 401, 401, 401, 401, 429]);
});

test("login limiter uses a true rolling window", () => {
  const realNow = Date.now;
  let now = 120_000;
  Date.now = () => now;
  try {
    assert.equal(limit([["rolling-boundary", 3, 60]]), 0);
    now = 121_000;
    assert.equal(limit([["rolling-boundary", 3, 60]]), 0);
    now = 179_000;
    assert.equal(limit([["rolling-boundary", 3, 60]]), 0);
    assert.equal(limit([["rolling-boundary", 3, 60]]), 1);

    now = 180_000; // only the first event expires; there is no boundary reset.
    assert.equal(limit([["rolling-boundary", 3, 60]]), 0);
    assert.equal(limit([["rolling-boundary", 3, 60]]), 1);
  } finally {
    Date.now = realNow;
  }
});

test("valid login: forwarded with the key, without the cookie; traversal refused", async () => {
  const r = await call(login, "/api/login", {
    method: "POST",
    headers: { "content-type": "application/json", "x-real-ip": "3.3.3.3" },
    body: JSON.stringify({ username: "owner", password: "correct horse battery" }),
  });
  assert.equal(r.status, 200);
  const cookie = r.headers.get("set-cookie").split(";")[0];
  assert.match(r.headers.get("set-cookie"), /HttpOnly; Secure; SameSite=Strict/);

  forwarded.length = 0;
  const ok = await call(proxy, "/api/proxy?p=captures", { headers: { cookie } });
  assert.equal(ok.status, 200);
  assert.equal(forwarded[0].url, "https://vm.example/v1/captures");
  assert.equal(forwarded[0].init.headers.get("authorization"), "Bearer VM_KEY_SECRET");
  assert.equal(forwarded[0].init.headers.get("cookie"), null);

  forwarded.length = 0;
  assert.equal((await call(proxy, "/api/proxy?p=..%2Fopenapi.json", { headers: { cookie } })).status, 400);
  const foreign = await call(proxy, "/api/proxy?p=captures", {
    method: "POST",
    headers: { cookie, origin: "https://evil.example" },
  });
  assert.equal(foreign.status, 403);
  assert.equal(forwarded.length, 0);
});
