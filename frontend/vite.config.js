import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// 构建产物 frontend/dist 由 FastAPI StaticFiles 托管；
// dev 模式把后端端点前缀代理到本地 8000。漏在表里的前缀会以「vite 自己的 404」
// 出现（不是后端的 detail），所以这张表要跟 agent_framework/server.py 的路由前缀对齐。
const BACKEND = "http://127.0.0.1:8000";
const PREFIXES = [
  "/chat", "/convs", "/runs", "/sessions", "/settings", "/tools",
  "/register", "/whoami", "/health", "/materials", "/bgm", "/upload",
  "/fetch_media", "/timelines", "/render_direct", "/render_status", "/latest_timeline",
  "/motion",
];

export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: Object.fromEntries([
      ...PREFIXES.map((p) => [p, { target: BACKEND, changeOrigin: true }]),
      ["/ws", { target: BACKEND, ws: true }],
    ]),
  },
});
