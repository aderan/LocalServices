import { defineConfig } from "vite";

export default defineConfig({
  server: {
    port: 18210,
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:18211",
    },
  },
  preview: {
    port: 18210,
    strictPort: true,
  },
});
