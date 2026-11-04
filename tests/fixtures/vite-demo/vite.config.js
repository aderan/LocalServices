import { defineConfig } from "vite";

export default defineConfig({
  server: {
    port: 18210,
    strictPort: true,
    // Locove 代理入口：显式允许的 Host（绝不把 allowedHosts 设为 true）
    allowedHosts: ["vite-demo.locove"],
    proxy: {
      "/api": "http://127.0.0.1:18211",
    },
  },
  preview: {
    port: 18210,
    strictPort: true,
  },
});
