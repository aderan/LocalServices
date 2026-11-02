# tests/fixtures — Runtime/Resources 线（V2）人工验证骨架

只提交小文本文件：**不含** node_modules / .venv / go 构建产物 / 任何二进制与锁文件，
**不含任何密钥**（无 token/password/api key；全部为公开演示内容）。

端口统一走测试网段 18200-18299，绝不触碰 8000/5176/47800。

| 目录 | 端口 | 就绪探针 | 准备步骤（仅 N04，经用户可见计划执行） |
|---|---|---|---|
| `vite-demo/` | 18210（dev server；`/api` 代理到 18211） | `http://127.0.0.1:18210/`（任意 2xx/3xx） | `npm install`（生成 node_modules，不入库） |
| `fastapi-demo/` | 18212 | `http://127.0.0.1:18212/health` → `{"ok": true}` | `uv sync`（生成 .venv，不入库） |
| `go-demo/` | 18213 | `http://127.0.0.1:18213/health` → `{"ok": true}` | `go build`（产物不入库；也可直接 `go run .`） |
| `compose-demo/` | 18214（宿主）→ 80（容器） | `http://127.0.0.1:18214/`（busybox httpd 返回 'ok'） | 无（镜像 alpine:3.20 本机已拉取，离线可跑） |

## 各 fixture 说明

- **vite-demo**：`package.json` devDependency 仅 `vite ^7`（无锁文件）；`npm run dev`
  = `vite --port 18210 --strictPort`；`vite.config.js` 将 `/api` 代理到
  `http://127.0.0.1:18211`（预留位：可再挂一个后端验证代理链路）；页面为静态文本 +
  容错的 `/api/health` fetch。
- **fastapi-demo**：`pyproject.toml`（dependencies: fastapi, uvicorn；无锁文件）；
  `app/main.py` 暴露 `GET /health`。启动：
  `uv run --directory <dir> uvicorn app.main:app --port 18212`。
- **go-demo**：`go.mod`（module godemo, go 1.24）+ `main.go`，仅标准库，
  `GET /health` 返回 `{"ok":true}`，监听 `127.0.0.1:18213`。
- **compose-demo**：单服务 `web`，镜像 `alpine:3.20`（本机已有，避免拉取），
  用 busybox `httpd` 起静态服务；**故意不配置 docker healthcheck** —— 就绪判定
  依赖宿主端口探针（18214），用于验证 V2.7 “无 health 用端口探针” 的路径。

## 对应清单字段（人工验证 TOML 示例要点）

- vite/fastapi/go 组件：普通 `run = "local"`（cmd + ready.http）。
- compose-demo 组件：`run = "compose"`、`compose_service = "web"`、
  `compose_project` 缺省（= cwd 目录名 `compose-demo`）、`compose_files` 缺省
  （自动探测 `docker-compose.yml`）、`provision = "existing"`（只 start 已有容器；
  `up` 需注册时显式 `confirmed_provision=true`）。
