import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The desktop shell supplies the API base at runtime; no credentials and no
// model ids ever reach the client.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { port: 5173, strictPort: true },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
