/**
 * 页面入口。
 *
 * 注意：这里刻意不包 React.StrictMode —— 它在开发环境会把 effect 跑两遍，
 * 而图片懒加载是"每挂载一次就入队一次"的命令式副作用，双跑会让同一张图
 * 请求两次，正好撞上道旅 CDN 的并发风控（表现为随机 403）。
 */

import { createRoot } from "react-dom/client";

import App from "./App";
import "./styles.css";

const container = document.getElementById("root");
if (!container) throw new Error("缺少 #root 挂载点，index.html 是否被改动？");

createRoot(container).render(<App />);
