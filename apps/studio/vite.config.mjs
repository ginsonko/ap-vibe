import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  build: {
    outDir: "dist/client",
  },
  optimizeDeps: {
    include: ["react", "react-dom/client"],
  },
  server: {
    host: "0.0.0.0",
    allowedHosts: ["terminal.local"],
    proxy: {
      "/v1": {
        target: process.env.AP_VIBE_DEV_BACKEND || "http://127.0.0.1:8765",
        changeOrigin: false,
      },
    },
    warmup: {
      clientFiles: ["./src/main.jsx"],
    },
  },
  plugins: [react()],
});
