// vite-demo：静态渲染即可（离线可跑）；页面本身不依赖后端。
const app = document.querySelector("#app");
app.textContent = "vite-demo ok (port 18210)";
// 可选：若 18211 上有服务，则把 /health 结果附加展示（失败静默）。
fetch("/api/health")
  .then((r) => (r.ok ? r.json() : null))
  .then((v) => {
    if (v) app.textContent += ` /api/health -> ${JSON.stringify(v)}`;
  })
  .catch(() => {});
