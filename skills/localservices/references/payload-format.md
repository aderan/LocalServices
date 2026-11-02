# 注册 payload 字段说明

`lsctl register` 的 body（结构权威：仓库 `schemas/register.schema.json`；语义权威：`docs/CONTRACTS.md` §2）。
顶层字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `schemaVersion` | int | 恒为 `1` |
| `revision` | int ≥ 0 | 目标 Project 当前修订号。项目已存在且要更新时必填（否则 409）；新建省略（缺省 0 = 仅当不存在时创建） |
| `project` | object | `{id?, name?, agent?}`；`id` 匹配 `^[a-z0-9][a-z0-9-]*$` 且稳定不改；`agent` ∈ codex / claude / none |
| `directories` | array | `[{"path": 绝对路径, "role": "source"}]`；注册校验要求 `source` 与全部组件 `cwd` 落在声明目录范围内 |
| `service` | object | 服务定义，见下 |
| `draft_id` | string \| null | 转正草稿时填 |

`service` 字段：`id`（同 project id 规则，缺省从 name 派生）、`name`、`description`、`source`（工程绝对路径，必填）、`open`（启动后打开的 URL，可为 null）、`autostart`、`adapter`、`observe_only`、`draft`、`project_hint`、`components[]`、`dependencies[]`、`evidence`、`edited[]`、`accept_new[]`、`confirmed_provision`（V2）。服务器只接受这些键，未知键整单拒绝。

V2 覆盖层字段（V2.3）：

- `service.edited: [dotted paths]`——本次人工确认改过的字段（如 `"name"`、`"components[api].port"`）。注册成功（created/updated）后这些路径的最终值写入覆盖层，之后重新识别/注册会优先应用覆盖值（dry_run diff 也在应用覆盖后计算，预览=实际）。
- `service.accept_new: [paths]`（或 `["*"]`）——显式清除指定覆盖（"采用新值"）。仅在用户明确放弃旧值时携带。

V2 compose 字段（V2.7）：组件可带 `run = "compose"`（此时无 `cmd`，不校验 cmd）、`compose_service`（必填）、`compose_project`（缺省 = cwd 目录名）、`compose_files[]`（缺省自动探测）、`provision = "existing" | "up"`。`provision = "up"` 允许创建/拉取容器，注册 payload 必须显式 `service.confirmed_provision = true`（用户已知情同意），否则 400 拒绝。

`components[]` 常用字段：`id`（`^[a-z0-9][a-z0-9_-]*$`）、`kind`（server / worker / generator）、`name`、`cmd`（**argv 数组**，`cmd[0]` 须可解析：绝对路径 / PATH / `$source/node_modules/.bin` / 相对 `$source`）、`shell_script`（替代 cmd：指向工程内已存在脚本，由 /bin/sh 执行）、`cwd`（相对 source）、`env` / `env_files`、`port`（1–65535，且不得与已加载清单冲突）、`ready`（`{http?, tcp?, json_ok?, timeout?}`，探针端口须与本组件 port 一致）、`required`、`after`（先启动的组件 id）、`hot` / `watch` / `restart_on` / `ignore`、`build` / `install` / `install_when` / `install_marker`、`auto_update`、`external_cmd` / `external_cwd` / `external_cwd_exact`、`start_timeout`、`debounce`。

`dependencies[]`：`name`、`probe`（`{http?, tcp?, json_ok?, timeout?}`）、`affects`（受影响的组件 id）、`required`、`managed`、`container` / `volume` / `resource`（如 `"container:xxx"`，引用不存在时降级为 warning）、`note`。

`evidence`：`{agent, summary, files[], analyzed_at, unresolved[]}`——记录依据来自哪些文件、哪些问题没解决。端口/探针没把握就写进 `unresolved`，不要编造。

草稿：`service.draft = true`，或缺 `name` / `source` / 组件 `cmd`（或 shell_script）时，服务存为草稿（返回 `draft_id`，不进 services/，不可启动）。

幂等身份 = `source` + 组件 `cwd` 集合 + `cmd` argv 集合：同 id 同身份同内容 → `unchanged`；同身份不同 id（同 Project）→ 返回既有 id，`duplicate`；同 id 不同身份 → 需正确 `revision`，否则 409（响应含 `current_revision`）。

## 示例：Makefile 入口服务（含依赖与探针依据）

```json
{
  "schemaVersion": 1,
  "revision": 0,
  "project": {"id": "demonotes", "name": "DemoNotes", "agent": "codex"},
  "directories": [{"path": "/Users/flb/Sources/Products/DemoNotes", "role": "source"}],
  "service": {
    "id": "demonotes-admin",
    "name": "DemoNotes Admin",
    "description": "Makefile 驱动的本地管理端：make serve-api / make serve-admin",
    "source": "/Users/flb/Sources/Products/DemoNotes",
    "open": "http://127.0.0.1:4928/",
    "components": [
      {
        "id": "api",
        "kind": "server",
        "name": "API · 4927",
        "cmd": ["make", "serve-api"],
        "port": 4927,
        "ready": {"http": "http://127.0.0.1:4927/api/health", "json_ok": "ok", "timeout": 10},
        "watch": ["server/src"],
        "hot": true
      },
      {
        "id": "admin",
        "kind": "server",
        "name": "管理端 · 4928",
        "cwd": "admin",
        "cmd": ["make", "serve-admin"],
        "port": 4928,
        "ready": {"http": "http://127.0.0.1:4928/", "timeout": 15},
        "after": ["api"]
      }
    ],
    "dependencies": [
      {
        "name": "postgres",
        "probe": {"tcp": "127.0.0.1:5434", "timeout": 3},
        "affects": ["api"],
        "required": true,
        "resource": "container:demonotes-postgres",
        "note": "docker compose 起在 5434，见 README「数据库」与 compose ports 映射"
      }
    ],
    "evidence": {
      "agent": "codex",
      "summary": "Makefile 定义 serve-api(4927)/serve-admin(4928)；README 端口表；compose 把 postgres 5432 映射到 5434",
      "files": ["Makefile", "README.md", "docker-compose.yml"],
      "analyzed_at": null,
      "unresolved": ["admin 组件是否需要 NODE_ENV=development：文件中未找到依据，未填写"]
    }
  }
}
```

示例中的端口、探针、依赖全部有文件依据；换到真实工程时同样标准：**没有依据的字段留空或进 `unresolved`，有依据才填**。
