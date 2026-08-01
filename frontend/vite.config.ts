import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the API lives on :8000 — proxy /api there.
// In production the built assets are served by FastAPI itself.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
