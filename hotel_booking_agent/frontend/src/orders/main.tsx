/**
 * 订单页入口（Vite 多页构建的第二个入口，产物是 dist/orders.html）。
 *
 * 没有引入前端路由：聊天页和订单页各自一个 HTML 入口，
 * /orders 这个路径仍然归 FastAPI 管，深链不会和前端路由打架。
 */

import { createRoot } from "react-dom/client";

import OrdersPage from "./OrdersPage";
import "./orders.css";

const container = document.getElementById("root");
if (!container) throw new Error("缺少 #root 挂载点，orders.html 是否被改动？");

createRoot(container).render(<OrdersPage />);
