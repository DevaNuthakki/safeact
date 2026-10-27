/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The console is served from the same origin as the API: the dev server
// proxies every API path to uvicorn, so cookies stay first-party and no
// CORS is needed. SAFEACT_API_URL points the proxy elsewhere (E2E).
const api = process.env.SAFEACT_API_URL ?? "http://127.0.0.1:8000";
const apiPaths = [
  "/auth", "/actions", "/grants", "/workflows", "/proposals", "/events",
  "/healthz", "/readyz",
];

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: Object.fromEntries(apiPaths.map((path) => [path, {
      target: api,
      changeOrigin: false,
      // The console's pages share paths with the API (/actions/<id>).
      // A browser navigation asks for HTML and gets the app; the
      // console's own fetch and EventSource calls go to the API.
      bypass: (req: { headers: Record<string, string | string[] | undefined> }) => {
        const accept = String(req.headers.accept ?? "");
        return accept.includes("text/html") ? "/index.html" : undefined;
      },
    }])),
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true,
  },
});
