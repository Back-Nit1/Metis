// Print a METIS_LOGIN_PASSWORD_HASH for the Vercel login. The password is read
// from stdin so it stays out of shell history:
//   node ops/vercel/hash-password.mjs | vercel env add METIS_LOGIN_PASSWORD_HASH production
import { pbkdf2 } from "./api/_auth.js";

const ITERATIONS = 210_000;
let password = "";
for await (const chunk of process.stdin) password += chunk;
password = password.replace(/\r?\n$/, "");
if (password.length < 12) {
  console.error("use a password of at least 12 characters");
  process.exit(1);
}
const salt = crypto.getRandomValues(new Uint8Array(16));
const saltB64 = Buffer.from(salt).toString("base64url");
process.stdout.write(`pbkdf2_sha256$${ITERATIONS}$${saltB64}$${await pbkdf2(password, salt, ITERATIONS)}\n`);
