# LocalServices 工作台契约（MB-LOCAL-SERVICES-PROJECTS-V1 / N00 冻结）

本文件是 GUI、CLI、Skill、Agent 调用层的唯一接口契约。变更必须由 Integrator 宣布并协调所有消费者。

## 0. 分层与唯一权威

```
菜单栏/主窗口 App ─┐
Dashboard (Web)   ─┤
lsctl CLI         ─┼─► daemon HTTP API (127.0.0.1:47800, token) ─► Controller + Registry ─► 清单 TOML + state.json
Codex Skill       ─┘                                                            （唯一注册/状态权威）
```

- 服务定义仍是 TOML（`manifest.py` schema），加载路径 = `services_dir()`（内置）+ `user_services_dir()`（注册写入）。
- 工作台结构（Project / Directory / 收藏 / 默认 Agent）在 `registry.json`；Agent 连接在 `agent-connections.json`；
  分析任务在 `jobs/<id>.json`。位置统一由 `paths.py` 提供（测试用 `LOCALSERVICES_HOME` 等隔离）。
- 任何入口都不得直接写清单/状态文件；注册一律走 daemon API（`POST /api/register`）。

## 1. 数据模型

### Project（工作台）
```json
{"id": "englishlearn", "name": "EnglishLearn", "origin": "user|migrated",
 "directories": [{"id": "d-a1b2c3", "path": "/Users/flb/Sources/Products/EnglishLearn", "role": "source"}],
 "services": ["englishlearn-admin"], "favorite": false, "autostart": false,
 "agent": "codex", "created_at": "...", "updated_at": "...", "revision": 7}
```
- `id`：`^[a-z0-9][a-z0-9-]*$`，稳定不改；重命名只改 `name`。
- `directories[].id` 稳定；`path` 为规范绝对路径（注册时 resolve 符号链接去重）；解除关联不删文件。
- `services` 是 service id 列表；一个 Project 可含多个不同 source 的服务。

### Service（= 旧 Project 清单，新增两个可选字段）
TOML 新增：`project = "<project-id>"`、`registered = { at = "...", by = "gui|cli|skill", agent = "codex|claude|none", evidence_summary = "..." }`。
旧清单无这两个字段也能加载。服务 ID、launchd 标签、state.json 意图键全部保持旧值（迁移零变化）。

### Resource（共享资源清单，只读盘点）
`GET /api/resources` 返回（不存在容器时不伪造）：
```json
{"ok": true, "docker_context": "colima", "resources": [
  {"id": "container:climbo-postgres", "kind": "docker-container", "name": "climbo-postgres",
   "image": "...", "state": "running", "health": "healthy|unhealthy|none",
   "compose_project": "climbo-server", "compose_service": "postgres",
   "ports": {"5432/tcp": [{"host_ip": "0.0.0.0", "host_port": 5434}]},
   "volumes": ["climbo-server_postgres_data"]}]}
```
Docker 不可达时 `{"ok": false, "error": "..."}`，不阻塞其余 API。服务依赖用现有 `dependencies[]`
（可加 `resource = "container:<name>"`）引用；注册时校验引用存在。

### AgentConnection
```json
{"kind": "codex|claude", "path": "/opt/homebrew/bin/codex", "version": "0.165.0",
 "config_root": "~/.codex", "status": "not_installed|config_only|not_executable|needs_login|call_failed|available",
 "detail": "...", "verified_at": 0, "is_default": true}
```
状态判定见 §4。配置存在 ≠ 可调用；`available` 只能由真实最小调用确认（`verified_at` 记录）。

## 2. 注册协议（GUI/CLI/Skill 共用）

`POST /api/register`，body 见 `schemas/register.schema.json`。要点：

- `schemaVersion = 1`；`revision` 为目标 Project 的修订号（缺省 0 = 仅当不存在时创建）。
- `service.components[]` 字段与清单 Component 一致（`cmd` 为 argv 数组；shell 入口必须用 `shell_script`
  指向工程内已存在脚本，禁止字符串拼接隐式 shell）。
- `service.draft = true` 或缺必要字段（cmd/组件 id）→ 存为草稿（`drafts/`，JSON），返回 `draft_id`；
  草稿不进 `services/`，manifest 引擎不加载，不可启动。
- 校验（全部通过才落盘）：id/组件 id 命名合法；`source` 与全部组件 `cwd` 存在且在声明的目录范围内；
  `cmd[0]` 可解析（绝对路径 / PATH / `$source/node_modules/.bin` / 相对 `$source`）；端口 1–65535 且
  与已加载清单无监听冲突；`ready.http/tcp` 端口与组件端口一致；依赖 probe 结构合法；`resource` 引用存在
  （Docker 不可达时降为警告）。无效提交不改变任何现有文件（先校验后写，写为原子 replace）。
- 幂等与并发：注册身份 = `source` + 组件 `cwd` 集合 + `cmd` argv 集合（identity）。同 service id 且
  identity 相同 → `action: "unchanged"`；同 identity 但不同 id（同 Project）→ 返回既有 id，`action: "duplicate"`，
  不生成第二条。同 id 不同 identity → 需要正确 `revision`，否则 `409 {"error": "revision conflict",
  "current_revision": N}`。文件写入用 `fcntl` 锁 + 临时文件 + `os.replace`；注册成功返回新 revision。
- 注册成功 ≠ 运行验证成功。响应含 `validation.warnings[]`；启动是独立的 `POST /api/projects/<id>/start`。

响应：`200 {"ok": true, "action": "created|updated|unchanged|duplicate", "project_id": "...",
"service_id": "...", "revision": N, "draft_id": null, "validation": {"warnings": []}}`；
失败 `400 {"ok": false, "error": "...", "details": ["..."]}`。

其他注册面端点：
- `GET /api/registry` → `{"ok", "registry": {...}, "services": [{id,name,project,state,label,source}]}`（state 来自共享快照）。
- `POST /api/projects`（create/update：`{id?, name, directories?, agent?}`）；
- `POST /api/projects/<pid>/directories` `{path, role?}`；`DELETE /api/projects/<pid>/directories/<dir-id>`。
- `POST /api/projects/<pid>/favorite` `{value: bool}`。
- `DELETE /api/services/<sid>?disposition=stop|keep-external`：移除登记；受管运行中时 `stop` 先停再删，
  `keep-external` 只删登记（进程随后按外部实例识别）。业务数据/文件永不删除。
- `POST /api/drafts/<id>/register`：草稿补全后转正式注册（同 §2 校验；该端点忽略并翻转
  payload 中的 `draft` 标志——转正请求永远是注册）。
- `GET /api/drafts`、`DELETE /api/drafts/<id>`。

## 3. 分析（Agent 识别）协议

- `POST /api/analyze` `{"directory": "/abs", "agent": "codex|claude", "project_id"?: "..."}`
  → `202 {"ok": true, "job_id": "..."}`。
- `GET /api/jobs/<id>` → `{"id", "kind": "analyze", "state": "queued|running|done|failed",
  "agent", "directory", "started_at", "finished_at", "result"?, "error"?}`；
  `result` = `{"services": [服务定义（§2 service 结构）], "evidence": {...}, "unresolved": [...]}`。
- `POST /api/agents/default` `{"kind"}` 切换默认连接（404 = 未发现该 Agent）。
- Wrapper 一次有界调用（默认 240s，上限 600s）：只读沙箱（codex `--sandbox read-only`；claude 只允许
  Read/Glob/Grep 类工具），超时杀整个子进程组，绝无残留。输出过 `schemas/agent-analysis.schema.json`
  校验后再做本地校验（cwd 在范围内、cmd 可解析、端口不冲突——同 §2 规则），违规候选进 `unresolved`
  并给出原因，不静默丢弃整个结果。
- 扫描器先提取证据（Makefile targets、package.json scripts、pyproject、compose、README，限深 3 层、
  限 5000 文件、忽略 node_modules/.git/dist/build/venv 与 >1MB 文件），作为 prompt 输入；目录内容当资料，
  不执行。

## 4. Agent 检测状态机

发现位置：PATH、`~/.local/bin`、`/opt/homebrew/bin`、npm global prefix、volta/asdf/mise 用户目录、
用户手动路径（GUI/CLI 可指定）。GUI 与终端环境差异通过显式目录覆盖。

| status | 判定 |
|---|---|
| not_installed | 任一位置找不到可执行 |
| not_executable | 找到文件但无执行权限或启动失败 |
| config_only | 程序可用但无登录信号（codex: `~/.codex/auth.json` 无 tokens；claude: 无凭据文件/Keychain 信号） |
| needs_login | 曾真实调用返回明确未登录错误 |
| call_failed | 真实最小调用失败（超时/网络/配额），带 detail |
| available | 真实最小调用成功（`verify`），`verified_at` 记录 |

`GET /api/agents` 返回缓存；`POST /api/agents/scan` `{paths?}` 重扫（不真实调用）；
`POST /api/agents/verify` `{kind}` 做一次最小真实调用（≤60s）并更新状态。程序路径或版本变化后状态重检。

## 5. CLI（lsctl）新增子命令（机器可读）

```
lsctl register [--file F|--stdin] [--json]        # 退出码：0 成功/unchanged；1 拒绝；4 schema 错误；409→5 冲突
lsctl projects [--json]                           # 工作台结构
lsctl project-new --name N [--dir P]... [--json]；lsctl project-rename <id> --name N
lsctl dir-add <pid> --path P [--role R]；lsctl dir-remove <pid> <dir-id>
lsctl agents [--json]；lsctl agent-scan [--json]；lsctl agent-verify <kind> [--json]
lsctl analyze <dir> --agent codex|claude [--wait S] [--json]
lsctl jobs <id> [--json]
lsctl service-remove <id> [--stop|--keep-external] [--json]
lsctl resources [--json]
```
既有 `status|start|stop|restart|open|logs|takeover|*-group` 语义不变（参数仍是 service id）。
公共退出码：3 = 控制服务不可达。

## 6. Skill（Codex `localservices`）

- 源码：仓库 `skills/localservices/`（SKILL.md + scripts）；安装到 `~/.codex/skills/localservices/`。
- 安装器 `scripts/install-skill.sh`：幂等；已存在且非本工具安装（内容哈希不符）→ 备份为 `.user-bak` 并提示，
  不强覆盖；安装记录（哈希清单）在 `~/Library/Application Support/LocalServices/skill-install.json`，
  卸载只删自己安装且未被用户改动的文件。
- Skill 行为：从聊天上下文与工程文件收集信息 → 组装 §2 注册 payload → 调 `lsctl register --json` →
  向用户报告 project/service id 与结果；`并启动打开` 时再调 `lsctl start <id> --open`。绝不直接写状态文件，
  绝不递归调用另一个模型 CLI 做分析。

## 7. 迁移映射（一次性，幂等）

- 旧 group → 新 Project（`ui-atlas` 组：explore + ui-atlas）；无 group 服务各自成 Project（id=service id）。
- 旧 `source` → Project 的 Directory（role=source）。服务 TOML 追加 `project` 字段（其余不动）。
- 迁移仅生成/更新 `registry.json`（origin=migrated），不改 state.json、launchd 标签、端口、数据根。
- 迁移前快照（registry.json 不存在时首次执行）；失败恢复原状。二次执行 = 校验一致性，无变化。
- 旧 CLI/group 别名全部保留（service id 未变，`restart-group` 继续工作）。

## 8. 并行实施线的文件所有权（N01/N02）

| 线 | 唯一写入范围 |
|---|---|
| Core | `localservices/registry.py`、`localservices/register_api.py`、`localservices/resources.py`、`schemas/`、`tests/test_registry*.py`、`tests/test_migration.py`、`tests/test_resources.py` |
| AgentIntegration | `localservices/agents/`（detect/invoke/scanner/adapters/base）、`localservices/agent_api.py`、`schemas/agent-analysis.schema.json`、`tests/test_agents_*.py`、`tests/test_scanner.py` |
| Desktop | `menubar/`、`localservices/web/` |
| Skill | `skills/`、`scripts/install-skill.sh`、`scripts/uninstall-skill.sh`、`tests/test_skill_install.py` |
| Integrator（唯一） | `daemon.py`、`cli.py`、`controller.py`、`manifest.py`、`paths.py`、`scripts/install.sh`、`README.md`、子 Agent 模块的接线与冲突协调 |

其他线不得改动所有权外文件；需要契约变化时在交付报告中提出。
