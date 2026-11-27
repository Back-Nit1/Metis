import { AuthError } from "./auth";

const HEADER = "X-Metis-Session";

async function sessionRequest(method: "GET" | "POST" | "DELETE", headers?: HeadersInit): Promise<Response> {
  const response = await fetch("/v1/session", { method, headers, credentials: "same-origin", cache: "no-store", redirect: "error" });
  if (response.status !== 204 && response.status !== 401) {
    throw new AuthError("Browser sign-in is unavailable. Try again later.");
  }
  return response;
}

export async function browserSession(idToken?: string): Promise<string | null> {
  const response = await sessionRequest(idToken ? "POST" : "GET", idToken ? { Authorization: `Bearer ${idToken}` } : undefined);
  if (response.status === 401) return null;
  const binding = response.headers.get(HEADER);
  if (!binding || !/^[a-f0-9]{64}$/.test(binding)) throw new AuthError("The browser session could not be verified.");
  return binding;
}

export async function endBrowserSession(binding: string): Promise<void> {
  await sessionRequest("DELETE", { [HEADER]: binding });
}
