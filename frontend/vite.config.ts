import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import basicSsl from "@vitejs/plugin-basic-ssl";

// API client, wire types, copy and theme tokens shared with the mobile app.
const shared = fileURLToPath(new URL("../shared/src", import.meta.url));

// VITE_HTTPS=1 enables a self-signed cert so getUserMedia works on a phone
// against the dev server (camera APIs require a secure context).
export default defineConfig({
  plugins: [react(), ...(process.env.VITE_HTTPS ? [basicSsl()] : [])],
  resolve: {
    alias: { "@shared": shared },
  },
  server: {
    port: 5173,
    host: true,
    // ../shared lives outside the Vite root; let the dev server read it.
    fs: { allow: [".", shared] },
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
