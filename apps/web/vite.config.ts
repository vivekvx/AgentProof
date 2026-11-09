import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
const proxyHeaders = process.env.AGENTPROOF_API_TOKEN
  ? { Authorization: `Bearer ${process.env.AGENTPROOF_API_TOKEN}` }
  : undefined;
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    allowedHosts: ["web"],
    proxy: {
      "/api": {
        target: process.env.API_PROXY_TARGET || "http://127.0.0.1:8080",
        headers: proxyHeaders,
      },
      "/healthz": {
        target: process.env.API_PROXY_TARGET || "http://127.0.0.1:8080",
        headers: proxyHeaders,
      },
    },
  },
});
