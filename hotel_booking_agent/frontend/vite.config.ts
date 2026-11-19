import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * 开发：npm run dev（:5173），/api 与 /orders 都代理到 python web.py（:8000）。
 * 生产：npm run build 出 dist/，由 web.py 直接托管（同源，无需任何 VITE_* 变量）。
 */
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
      "/orders": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
