import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import basicSsl from "@vitejs/plugin-basic-ssl";

// VITE_HTTPS=1 enables a self-signed cert so getUserMedia works on a phone
// against the dev server (camera APIs require a secure context).
export default defineConfig({
  plugins: [react(), ...(process.env.VITE_HTTPS ? [basicSsl()] : [])],
  server: {
    port: 5173,
    host: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
