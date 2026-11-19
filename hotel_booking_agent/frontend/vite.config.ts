import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/**
 * 多页构建：index.html（聊天页）+ orders.html（订单页）。
 *
 * 不引入前端路由：两个页面各自一个入口，/ 与 /orders 仍由 FastAPI 决定返回哪个 HTML，
 * 深链和书签不会和路由冲突。
 *
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
    rollupOptions: {
      input: {
        main: "index.html",
        orders: "orders.html",
      },
    },
  },
});
