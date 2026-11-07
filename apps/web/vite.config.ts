import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    allowedHosts: ["web"],
    proxy: {
      "/api": process.env.API_PROXY_TARGET || "http://127.0.0.1:8080",
      "/healthz": process.env.API_PROXY_TARGET || "http://127.0.0.1:8080",
    },
  },
});
