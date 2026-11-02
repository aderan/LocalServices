# tests/fixtures — 六开发模式人工验证夹具（V2/N04）

只提交小文本文件：**不含** node_modules / .venv / go 构建产物 / 任何二进制与锁文件，
**不含任何密钥**。端口统一 18200–18299，绝不触碰 8000/5176/47800。

| 目录 | 端口 | 就绪探针 | 准备步骤（仅 N04，按动作范围执行） |
|---|---|---|---|
| `vite-demo/` | 18210（dev server；`/api` 代理到 18211） | `http://127.0.0.1:18210/` | `npm install`（node_modules 不入库） |
| `fastapi-demo/` | 18212 | `http://127.0.0.1:18212/health` → `{"ok":true}` | `uv sync`（.venv/uv.lock 不入库） |
| `go-demo/` | 18213 | `http://127.0.0.1:18213/health` → `{"ok":true,"version":"…"}` | 无（manifest build 步骤构建；产物在隔离 home 的 build 目录） |
| `next-demo/` | 18218 | `http://127.0.0.1:18218/api/health` → `{"ok":true}` | `npm install`（node_modules/.next 不入库） |
| `compose-demo/` | 18214（宿主）→ 6379（容器） | `tcp 127.0.0.1:18214`（故意无 docker healthcheck → 端口探针路径） | 无（redis:7-alpine 本机已拉取，arm64 与 colima 匹配） |

## N04 真实验证得到的环境结论（写进夹具形状的原因）

- **node ≥17 绑定行为（F-1）**：`vite` 不带 `--host` 时 node v26 只绑 `[::1]`，
  `127.0.0.1` 探针永远连接拒绝。dev script 显式 `--host 127.0.0.1`（与 ShiCha 日常
  清单同法）。识别建议：Vite/Node 候选应显式 host/port，不用默认绑定。
- **本机 alpine:3.20 是 amd64 模拟镜像且 busybox 无 httpd applet（F-B2）**：compose
  夹具改用已在跑的 redis:7-alpine（tcp 探针同样验证“无 healthcheck → 端口探针”）。
- **DEFAULT_PATH 曾缺 goenv（F-3）**：`~/.goenv/shims` 已加入 manifest.DEFAULT_PATH
  （pyenv shims 同法）；项目仍可用绝对路径/自身 wrapper 覆盖。

## 对应清单字段要点

- vite/fastapi/go 组件：`run = "local"`（cmd + ready）。
- compose-demo 组件：`run = "compose"`、`compose_service = "web"`、`provision = "up"`
  需注册时显式 `service.confirmed_provision = true`（首次创建容器/拉镜像的用户确认）；
  `provision = "existing"` 只 start 已存在容器。

- **next-demo**：Next.js 15 App Router 最小工程；`npm run dev` = `next dev -H 127.0.0.1 -p 18218`。
  build 需在 dev 停止后执行（共享 .next 会冲突）——dev 与 build/start 是两种模式，不混跑。
