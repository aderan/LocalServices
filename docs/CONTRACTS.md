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
 "directories": [{"id": "d-a1b2c3", "path": "/Users/example/Projects/EnglishLearn", "role": "source"}],
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
- `verify_start: true` 可在正式注册前试启动新登记的本地服务，验证监听归属、探针和打开地址。通过后停止试运行进程并登记；失败保留验证记录，项目和服务定义不落盘。响应的 `trial` 提供状态、检查项和本机日志路径。
- `POST /api/services/<sid>/repair` 接收 `{revision, component: {id, port, cmd}}`，为已停止的服务验证并保存受限的端口修复。启动命令的端口参数、监听端口、探针与打开地址一起更新。
- 登记预检使用 `dry_run: true`。当 `action` 为 `diff` 时，`registration_action` 区分
  `created`、`updated` 和 `unchanged`；新服务的差异以空定义为基准。

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
  "agent", "directory", "started_at", "finished_at", "timeout"?, "activity"?, "output"?, "result"?, "error"?}`；
  `activity` 保留最近 16 条 `{event, at}`，`at` 为 Unix 秒。包含连接检查、扫描、CLI 启动、
  结果校验，以及 CLI 流中观测到的阶段。
  `output` 保留最近 100 条脱敏、截断的 `{text, at}`，来自 CLI 对外文本、检查动作和错误。
  添加服务页按任务独立读取这些记录。识别只创建任务，服务与新项目在确认登记后同事务写入。
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
- 旧 `source` → Project 的 Directory（role=source）。服务 TOML 追加 `project` 字段；内置清单需要补字段时，
  在用户目录生成副本并遮蔽内置清单。
- 迁移更新本机 `registry.json`（origin=migrated）和需要补字段的用户层清单，保留运行状态、launchd 标签、端口和数据根。
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

---

# V2 契约（MB-LOCAL-SERVICES-LIFECYCLE-V2 / N00 冻结）

本节由 V2 Goal 冻结，是增删恢复、覆盖层、资源与运行适配的唯一接口契约。
V1 §1–§8 继续有效；与之冲突的地方以本节为准。变更必须由 Integrator 宣布。

## V2.0 移除标记（tombstone）与归档

- `paths.removed_file()` = `home()/removed.json`；`paths.removed_dir()` = `home()/removed`；
  `paths.overrides_file()` = `home()/overrides.json`（paths.py 已接线）。
- `removed.json`：`{"schemaVersion":1,"services":{<sid>:{"removed_at","removed_by","disposition","project_id","archive"}},"projects":{<pid>:{"removed_at","removed_by","disposition","services":[sid...],"project":{...原 project 对象...}}}}`。
- 归档 `removed/<sid>-<ts>.json`：`{"sid","project_id","removed_at","disposition","toml":"<完整 TOML 文本>","state":{...state.json 该 sid 条目快照...},"overrides":{...该 sid 覆盖层快照...}}`。
- `manifest.load_all()` 跳过 tombstone 中 service id 对应的**内置目录**文件（`localservices/tombstones.py`
  提供 `services() -> set[str]`，读 removed.json，坏文件按空处理但不覆盖）。**移除内置服务不修改
  services_dir() 中随仓分发的模板**：builtin TOML 文件保持原样，仅登记层失效。
- **内置 id 遮蔽语义（修订）**：tombstone 只遮蔽 services_dir() 下的文件；user_services_dir() 的
  同 id 文件始终加载（用户层重新注册接管内置模板）。因此——
  - 重新注册的 id 若存在内置模板（如 shicha）：**保留** removed.json 条目（持续遮蔽内置文件），
    但 `GET /api/removed` 列表只显示当前未登记（load_all 不含该 id）的条目；
  - 重新注册纯用户层 id：成功后删除 removed.json 条目；
  - 编辑正在生效的内置清单：用户层副本与内置遮蔽条目一起提交。后续编辑同一服务继续更新用户层副本；
  - 迁移、升级、daemon 重启、reconcile 不复活 tombstone 服务（内置遮蔽持续生效）。

## V2.1 服务移除 v2 与预览

- `GET /api/services/<sid>/remove-preview` → `{"ok",service_id,name,state,running_components[],
  external_instances[],keep:[...],dispositions:{"stop":{...},"keep-external":{"enabled":bool,"reason"}},
  restore_hint}`。零副作用。
- `DELETE /api/services/<sid>?disposition=stop|keep-external` 语义：
  - `stop`：现有 stop-verified 流程不变，随后释放运行期对象（见 V2.4 清理表）。
  - `keep-external`：若任一组件仍由本工具 launchd 保活（loaded/running）→ `409`，错误信息说明
    "无法保留为外部实例：仍由本工具 launchd 保活，请选择停止后移除"；**绝不只删 TOML 留下孤儿
    KeepAlive job**。外部运行/已停止时正常移除。
  - 移除成功响应：`{"ok",service_id,project_id,disposition,archived,archive,released:[comp...],
    builtin:bool}`；失败 = 登记不变。
- 移除步骤：预检/停止 → 写归档 →（用户层 TOML 删除；builtin 不动文件）→ removed.json 加标记 →
  registry 去成员+revision → `ctl.release_service(sid)` 释放运行期对象；释放失败 → 409 登记保留
  （幂等可重试）。

## V2.2 Project 移除与恢复

- `GET /api/projects/<pid>/remove-preview` → `{"ok",project:{id,name},services:[{id,name,state,
  external_instances,dispositions{...}}],directories:[],resources_referenced:[{name,state,shared,
  consumers[]}],kept:[...],warnings[]}`；零副作用（含 `dry_run=true` 的 DELETE 同形返回）。
- `DELETE /api/projects/<pid>?disposition=stop|keep-external`：
  1. 逐成员按 V2.1 语义处置；任一成员停止/预检失败 → 整体中止：`{"ok":false,stopped:[...],
     remaining:[{id,reason}...]}`，**所有成员与项目的登记保留**（已停止进程不回拉）。
  2. 全部成功 → 成员各自归档+tombstone → removed.json 记 project 条目（含原 project 对象）→
     registry 删除项目。目录关联随项目消失；源码/业务数据/共享容器/卷一律不动。
  3. 空项目直接移除（仍可预览）。
- `GET /api/removed` → `{"ok",services:[{sid,project_id,removed_at,disposition,archive}],
  projects:[{pid,removed_at,services[]}]}`。
- `POST /api/removed/services/<sid>/restore` body `{project_id?,intent?:"stopped"|"running",
  revision?}`：清 tombstone → 归档 TOML 写回 **user_services_dir**（即使原是 builtin；后续移除
  走用户层）→ registry 恢复成员（目标项目缺省原 id，不存在则以归档名重建）→ state intent 显式
  设为所选值（缺省 stopped；**绝不从归档自动带回 running**）→ 不自动启动。同 id 已登记 → 409。
- `POST /api/removed/projects/<pid>/restore` body `{intent?}`：恢复项目条目 + 全部成员服务。
- **整项目恢复预检（V2 复核修订）**：写盘前对全部成员归档做端口/服务 id/注册身份预检（对当前
  清单与成员集合双向），任一冲突 → 409（details 逐条列出冲突对象）且零写入；成员归档 TOML 的
  `project` 字段统一重绑定为目标项目（跨项目恢复 `restore_service` 同理；一致时保持原字节）；
  多成员覆盖层合并为**单次** overrides.json 写入；`_atomic_commit` 拒绝同一事务的重复目标路径。

## V2.3 覆盖层（人工覆盖）

- `overrides.json`：`{"schemaVersion":1,"services":{<sid>:{"fields":{<dotted path>:value},
  "updated_at"}}}`；路径形如 `name`、`description`、`open`、`components[api].port`。
- 注册 payload `service.edited: [paths]`（GUI 在候选审核时 diff 用户编辑并携带）→ 注册成功
  （created/updated）后这些路径的**最终值**写入覆盖层。
- 注册合并：进入比较/写盘前，先把 `overrides[sid].fields` 应用到新定义（覆盖层优先）；payload
  `service.accept_new: [paths]`（或 `["*"]`）显式清除指定覆盖。**dry_run diff 在应用覆盖后计算**，
  预览=实际。
- **合并优先级（V2 复核修订）**：本次用户编辑（`service.edited` 列出的路径）> 旧人工覆盖 >
  模型候选——edited 路径旁路旧覆盖直接生效，成功后其最终值写回覆盖层（覆盖旧值）。
- **嵌套路径（V2 复核修订）**：覆盖/edited 路径支持任意嵌套 dotted path
  （`components[<cid>].<field>.<subfield>…`，如 `components[api].ready.http`、
  `components[api].env.DB_HOST`）；顶层仍限 name/description/open；缺失成员的路径忽略。
- `GET /api/services/<sid>/overrides` → `{"ok",sid,fields{}}`（GUI 显示"你的覆盖优先生效"与逐字段
  "采用新值"入口；采用新值 = 下次注册带 accept_new）。
- 移除时覆盖层随服务归档；恢复时带回。

## V2.4 dry_run 与读取/事务硬化

- dry_run + 不完整 payload → `{"ok":true,"action":"incomplete","missing":[...]}`，**不写草稿**；
  草稿仅由非 dry_run 请求产生。
- dry_run + 新 id 与既有服务同 identity → `{"ok":true,"action":"duplicate","service_id":<既有>,
  "diff":[...]}`，与实际行为一致。
- `_load`：JSON 合法但非对象 → 按损坏处理（corrupt-<ts> 备份 + 失败则 RegistryError 拒绝）；
  其他 OSError（权限等）→ RegistryError fail-closed，原文件不动、绝不当空库。
- 事务写 `_atomic_commit([(path,bytes)...])`：全部临时文件写好+fsync 后统一 rename；rename 失败
  用**原地写回**（r+b truncate+write+fsync，不新分配空间）恢复已 rename 文件的原字节。注册
  [TOML, registry.json] 与迁移回滚均走该助手——持续 I/O 故障下保持一致或整体失败，不靠再次
  分配空间写旧文件。

## V2.5 迁移修复

- TOML 已有**不同** `project` 值 → 跳过写入并在结果 notes 说明（绝不插第二个顶层键）。
- sid 已属其他项目 → 不挪归属、不双写成员（notes 说明）。
- tombstone 服务完全跳过。幂等语义保留。

## V2.6 资源 v2

- `localservices/dockerctx.py`（Runtime 线）：`name()` = env `LOCALSERVICES_DOCKER_CONTEXT`
  缺省 `"colima"`；`run(args,...)` 统一 `docker --context <name>` 并从子进程 env 清除
  `DOCKER_HOST/DOCKER_CONTEXT/DOCKER_CONFIG`；**绝不 `docker context use` 改全局**。resources /
  infrastructure / compose 三处同源。远程 endpoint（非本机 unix socket）→ 盘点只读允许，
  ensure/stop 拒绝。
- `GET /api/resources` → `{"ok","docker_context","resources":[...],"consumers":
  {<resource_id>:[{"service","via","running"}]}}`；inventory 用 `ps -a`（**含已停止容器**），
  字段新增 `state`(running|exited|created|paused)、`container_id`、`bind_mounts[{source,target}]`、
  `context`；保留 name/image/health/compose_project/compose_service/ports/volumes。
- 依赖 `container:` 引用校验改按 `ps -a`（已停止容器合法，附"已停止，启动时恢复"警告）。
- consumers 由清单（dependencies[].container + compose 组件）+ 当前运行状态推导。

### Docker 运行环境

- `GET /api/resources/environments` 返回本机 Docker 连接及运行环境。指向同一本机 socket 的连接合并展示。Colima 即使停止时移除了自己的 context，已有 Docker profile 仍会显示。每个环境单独报告状态，已登记的服务关系在离线时仍可见。`GET /api/resources` 继续提供默认 context 的容器清单。
- 注册可携带 `service.docker = {context, endpoint?, autostart?}`。Locove 解析并保存本机 socket，启动、停止、状态和日志按此绑定执行。连接改指向时，显式绑定要求重新选择。旧清单沿用 `LOCALSERVICES_DOCKER_CONTEXT`。
- Compose 和启动前需就绪的容器依赖共用环境恢复。显式绑定的 `autostart` 控制服务是否可以按需代启已确认的 Colima profile。资源页可用 `POST /api/resources/environments/start`，提交 `{context, endpoint}`，显式启动该 Colima 环境。容器创建仍由 Compose 的 `confirmed_provision` 单独授权。
- 停止服务保留 Docker 环境与依赖容器。若某个 Compose 组件的容器仍被另一条登记引用，停止该容器会返回引用方。

## V2.7 运行适配（LocalProcess + Compose）

- Component 新可选字段（manifest v2，向后兼容）：`run="local"|"compose"`（缺省 local）、
  `compose_service`（run=compose 必填）、`compose_project`（缺省=cwd 目录名）、
  `compose_files[]`（缺省自动探测 cwd 下 compose 文件）、`provision="existing"|"up"`（缺省
  existing=只 start 已存在容器；up=允许 `compose up -d --no-deps` 创建/拉取，**注册 payload 必须显式
  `service.confirmed_provision=true`，否则 400 并解释**）。run=compose 时无 `cmd`（不校验 cmd）。
- Controller：run=compose 组件不走 launchd：start=`compose.ensure_started`（existing→`docker start
  <container>`；up→`docker-compose -p X -f ... up -d --no-deps <svc>`）；readiness=容器 Running
  （有 health 用 health，无 health 有端口探针用探针，都没有=Running 即就绪并标注）；stop=
  `docker-compose ... stop <svc>`（**绝不 down/rm/删卷**）；logs=`docker logs`；status=inspect。
  LocalProcess 语义不变。`ctl.release_service(sid)`（V2.1 用）：bootout+删除
  `agents/<prefix>.<sid>.*.plist`+删 `run/<sid>.*.json`+删 state.json 该 sid 条目，幂等，返回
  `{ok,released[],errors[]}`。
- compose 组件的"外部实例"= 容器已在运行（up 幂等，无需接管杀树）；端口冲突遇到 docker 宿主代理
  进程（gvproxy/com.docker.backend/docker-proxy/vpnkit）→ 解析为容器归属，不误报进程冲突。
- **容器认领仅凭标签（V2 复核修订）**：compose 容器解析只认 project+service 标签；名字回退已
  移除——无标签匹配 → None（existing → 拒绝创建；up → compose up 创建带标签容器；stop/logs/state
  对 None 一律 no-op/missing），绝不按名字猜测操作归属不明的容器。
- **分析上下文脱敏（V2 复核修订，R01）**：项目上下文与证据在构造模型请求前递归脱敏——敏感键名
  一律掩码，env 映射只传变量名与非敏感值，argv 摘要逐元素脱敏，prompt 组装最后一道全文脱敏。

## V2.8 外部实例识别增强

- LAUNCHER_RE 扩展：`python3*`、`uv`、`uvicorn`、`make`、`go`、`air`、`just`、`task`（监督父链
  攀爬）；识别监督根再决定动作的语义不变，未知/无法证明归属 → unknown/conflict，不强停。
- 其余（端口/PID/cwd/命令匹配、probe 验证、显式接管预览）按 V1 语义。

## V2.9 自动启动

- `POST /api/projects/<pid>/autostart {value:bool}`（与 favorite 同形的专用端点）。
- 引擎：`Store.intent` 初始缺省 = running 当且仅当（服务 TOML autostart **或** registry 项目
  autostart）；registry 侧由 Core 提供 `Registry.autostart_map() -> {pid:bool}` 只读助手。

## V2.10 分析上下文与候选消歧

- `POST /api/analyze` 接受 `project_id`（404 校验）；prompt 增加上下文块：项目名/目录、既有服务
  （id/name/source/端口/组件摘要）、该服务覆盖层路径与值（清单层字段，非凭据）、资源盘点摘要
  （含已停止容器与卷）。CLI `lsctl analyze --project PID`。
- GUI 多目录联合识别：合并候选后同 id 冲突 → 自动分配稳定后缀 `-2`/`-3`（按目录顺序确定性），
  候选卡可改 id 并提示"多目录同名服务已分配稳定 ID"；登记用不同 id，杜绝第二次注册覆盖第一次。

## V2.11 GUI 增删入口

- Project 卡头 `⋯` 菜单：添加服务…/重新识别/重命名/移除项目…；Service 行 `⋯` 菜单：编辑/日志/
  打开目录/移除登记…。折叠区保留技术明细，不再承载移除动作。
- 移除对话框（自绘 modal，非 confirm()）：对象名称、将停止的实例、保留的目录/数据库/容器、受
  影响项目、恢复方式；键盘可达（Esc 取消、Enter 确认、focus trap）；轮询刷新不重建/关闭 modal
  （modal 打开时跳过整树 innerHTML 替换，仅更新数据）。
- 编辑对话框 → dry_run 注册 diff → 确认注册（带 edited 路径）；diff 预览中命中覆盖的字段显示
  "采用新值"（构造 accept_new）。

## V2.12 CLI 新子命令

```
lsctl project-remove <pid> [--stop|--keep-external] [--dry-run] [--json]
lsctl service-remove <id> [--stop|--keep-external] [--preview] [--json]   # 默认 keep-external 不变
lsctl removed [--json]；lsctl restore <sid> [--project PID] [--intent stopped|running] [--json]
lsctl project-restore <pid> [--intent stopped|running] [--json]
lsctl project-autostart <pid> --on|--off [--json]
lsctl analyze <dir> --agent A [--project PID] [--focus F] [--wait S] [--json]
```
退出码沿用 §5（0/1/3/4/5；preview/dry-run 成功=0）。

## V2.13 Skill

SKILL.md 增补：目标已有 Project（project-id/revision 读取）、移除/恢复命令（project-remove/
service-remove/restore）、失败不改写状态、绝不递归调用模型 CLI——与 GUI/CLI 同一后台协议。

## V2 并行线文件所有权

| 线 | 唯一写入范围 |
|---|---|
| Core/Lifecycle | `localservices/registry.py`、`register_api.py`、`tombstones.py`、`overrides.py`、`manifest.py`（load_all 过滤+compose 字段解析）、`schemas/`、`tests/test_removal*.py`、`test_overrides.py`、`test_registry_v2.py`、`test_migration_v2.py` |
| Runtime/Resources | `localservices/controller.py`、`launchd.py`、`procs.py`、`infrastructure.py`、`resources.py`、`runner.py`、`dockerctx.py`、`compose.py`、`tests/test_compose.py`、`test_resources_v2.py`、`test_external_v2.py`、`test_autostart_engine.py`、`tests/fixtures/` |
| Agent/UX | `localservices/agents/`、`agent_api.py`、`web/dashboard.html`、`cli.py`、`skills/`、`scripts/verify_v2_gui.cjs`、`tests/test_agents_v2.py`、`test_webui_v2.py` |
| Integrator（唯一） | `daemon.py`、`paths.py`、`docs/CONTRACTS.md`、`README.md`、`scripts/install*.sh`、冲突协调 |

既有测试文件（test_registry.py 等 V1 套件）任何人不得修改，必须持续保持绿；
新行为一律写新测试文件。

---

# V3 契约（MB-LOCALSERVICES-LOCOVE-DOMAINS / 2026-11-04 起）

本节由 Locove／洛湾 Goal 冻结。V1/V2 继续有效；冲突处以本节为准。产品显示名为 **Locove／洛湾**；
仓库名、内部包名、launchd 标签前缀 `com.mybrain.ls`、`lsctl` 命令、数据目录与既有服务 id/URL/API
全部保持兼容——品牌只落在窗口/菜单栏/工作台/安装产物说明等用户可见入口。

## V3.0 命名与品牌

- 所有主要产品入口呈现 Locove／洛湾；`LocalServices` 不得作为并列的第二产品身份展示。
- 迁移零变化：不改 launchd label、state.json 键、服务 id、CLI 名。

## V3.1 Endpoint 数据模型（manifest v3，向后兼容）

TOML 顶层新增可选 `[[endpoints]]`：`id`、`host`、`component`、`open_path`（缺省 `/`）。

- `host`：用户可见的稳定无端口域名；不含点号时补 `.locove` 后缀（本机命名约定，
  非公网 TLD；`LOCALSERVICES_DOMAIN_SUFFIX` 可重定向，仅测试/迁移用）。
- `component`：引用本服务组件 id，决定真实上游（该组件的实际监听端口）；未知引用在加载与注册时拒绝。
- `open_path`：打开页面的落点；整个应用路径/脚本/资源经同一 host 代理，不按 path 拆分。
- 旧清单无 `endpoints`：派生缺省入口 `host=<service-id>.locove`（锚定稳定 id，
  显示名称修改不影响）、`component`=open 端口对应组件（否则第一个有端口的 server 组件）、
  `open_path`=open URL 路径。无可浏览端口（纯 worker）则无入口。
- `Project.public_urls()` 返回 `http://<host><open_path>` 列表；`open` 保留为直连地址。

## V3.2 域名分配与冲突

- 跨服务重复 host（含派生）在 `manifest.load_all()` 拒绝加载（与端口冲突同级）。
- 注册（含 dry-run diff 预览=实际）在提交前拒绝与其他服务冲突的 host，错误列出占用方；
  同一服务自持 host 不受影响。恢复预检同样重查（V3.4）。
- 注册 payload `service.endpoints[]`（schema 已更新）；未提供时不写 TOML，由加载端派生。

## V3.3 依赖图与意图唯一写入者（结构性修正）

- 组件 `after` 未知引用 / 环：加载（`manifest.validate_component_graph`）与注册校验
  双侧拒绝，环错误报告完整链路（`a → b → a`）。
- 运行意图唯一写入口：daemon 内一切意图变更（含恢复流程）经 `Controller.set_intent_direct`
  → `Store`（内存+文件一致）；`Registry._apply_restore_intent` 无 ctl 写入口时回退原子直写。

## V3.4 无端口访问面（已实施；2026-11-04 review 修复后修订）

- 解析：`/etc/resolver/locove`（split DNS，安装需一次管理员授权）把 `.locove` 定向到
  用户级 DNS responder `com.mybrain.ls.locove.dns`（127.0.0.1:5354，UDP+TCP；
  已登记名 A=127.0.0.1 / AAAA·HTTPS=NODATA，未登记 NXDOMAIN；只管理本工具文件）。
  **resolver 文件所有权（R3）**：写入/删除经 `localservices.locove_resolver`，带
  `# managed-by: locove (com.mybrain.ls)` 标记；他人内容安装拒绝覆盖（且在任何系统文件
  写入之前，失败零残留）、卸载拒绝删除；旧版安装器写的无标记文件按旧头部认领可升级/卸载。
- 入口：`com.mybrain.ls.locove.entry`（root LaunchDaemon）绑定 127.0.0.1:80 后降权 nobody，
  仅向 127.0.0.1:47890 做字节转发；无 shell/配置/动态上游。
- 数据面：`com.mybrain.ls.locove.proxy`（Caddy）。**所有站点与 catch-all 经全局
  `default_bind 127.0.0.1 ::1` 只监听回环（R1）**；admin 固定 127.0.0.1。daemon 周期 sync
  （缺省 5s，另有 60s 强制重激活的漂移自愈）把「登记 × 运行事实」写入
  `~/…/LocalServices/locove/{routes.json,Caddyfile}`，
  先 `caddy validate` 再优雅 reload；validate 失败零写入，reload 失败回滚文件并保持运行态。
  停止服务 = 503 明确不可用页；移除登记 = 撤销路由（未知 Host 404，DNS NXDOMAIN）。
- **逐组件路由资格（R2）**：每个 endpoint 按其目标组件事实判定，不按服务总体状态——
  外部实例有已验证归属的实际端口 → 转发实际端口；组件 ready → 转发其端口；
  其余（stopped/starting/unhealthy/快照缺组件事实）→ unavailable（503）。
  partial 服务的 stopped 组件不会被代理，已停止组件的端口即使被无关程序占用也不转发。
- 外部实例：快照 `external_ports`（组件→实际监听端口）就绪时路由到实际端口；
  退出/未知占用 → unavailable，绝不转发到无关程序。
- 隔离守卫：`LOCALSERVICES_HOME` 重定向且未显式设置 `LOCOVE_ADMIN_PORT` 的进程只落盘，
  不得调用 Caddy 二进制（测试/验收脚本绝不污染生产实例）。
- 观测：`GET /api/locove` 与 `lsctl endpoints` 分层报告 dns/入口/代理健康与全部路由；
  80 占用/权限不足显示原因与安装指引。域名只代理业务服务；控制面（127.0.0.1:47800）的
  Host/Origin/token 规则不变，不因业务域名放宽。
- 安装/卸载：`scripts/install-locove-system.sh`（用户级自动；系统级 `--system` 需 sudo）/
  `scripts/uninstall-locove-system.sh`；幂等、只管理自身文件。**sudo 全安装的用户级步骤
  以目标用户身份执行（`launchctl asuser` + `sudo -u`），python 初始化显式
  `LOCALSERVICES_HOME=<目标用户状态根>`，agent plist 归目标用户（R4）。**
- 转发器顺序契约：root 绑定 80 → 降权 nobody → 用预绑定套接字服务（先降权后绑定会
  EACCES；升级 root 属主副本用 `scripts/refresh-locove-entry.sh`）。
- 代理兼容：系统代理环境需在绕行表加 `*.locove`（`scripts/locove-proxy-bypass.sh`，加法式），
  否则网关按公网域名上游解析而 502；`lsctl endpoints` 的 dns/入口层会如实呈现。

## V3.5 域名跳转为可选能力（用户修订 2026-11-04：默认本地 IP 直连）

- 工作台语言保存在 `registry.settings.language`（`zh-CN` 或 `en`，缺省英文）。
  `POST /api/settings/language` 接收 `{"language":"en"}`，成功返回 `{"ok":true,"language":"en"}`。
  工作台与菜单栏从 `GET /api/registry` 读取，界面语言不触发域名模块配置变更。
- registry.json 顶层 `settings`: `{"use_domains": bool（缺省 false）, "domain_form": "suffix"|"custom"（缺省 suffix）}`；
  `GET/POST /api/locove/settings`（POST 部分字段更新，非法 domain_form 400）；`GET /api/locove` 合并返回 `settings`。
- **打开行为**：默认（use_domains=false）一切打开动作用直连地址（`open` / 组件端口），
  即使域名入口健康也不经域名——「域名入口未就绪」提示只在开启后出现。
  开启 + 入口健康 → 经 `http://<host><open_path>` 打开；开启 + 入口故障 → 回退直连并附缺口说明。
- **域名形式**：`suffix` = 派生 `<id>.locove`（+显式）全部参与路由与跳转（V3.0–V3.4 行为）；
  `custom` = 仅显式登记的 `[[endpoints]]`（派生入口 derived=True 不参与路由/跳转/URL）。
  有显式 endpoints 的清单不派生（V3.1 不变）。
- **基础设施不变**：DNS/入口/代理与路由收敛持续运行（回环限定）；设置只影响工具的跳转与
  UI 主地址展示。手动在浏览器输入域名始终可用。
- 快照组件新增 `port`（直连地址与诊断直接可用，不依赖路由收敛节奏）；endpoints 新增 `derived`。
- **派生声明保真（R5）**：`GET /api/services/<sid>/definition` 的 endpoints 行带 `derived`
  标记（注册 payload 接受并忽略该字段）。既有服务无显式 `[[endpoints]]` 时，编辑往返
  （definition 原样回传）不把派生入口固化为显式声明——仅改 description 等无关字段不改变
  custom 模式的路由资格；与派生不一致（用户真正编辑 host/组件/落点）才写显式；
  全新注册显式提供的 endpoints 与既有显式入口不受影响。
- CLI：`lsctl locove-domains [--on|--off] [--form suffix|custom]`；`lsctl endpoints` 尾行显示跳转设置。

## V3.6 观测与调度分离（R7，2026-11-05）

- `Controller._refresh()` 是纯观测：采集运行事实并投影共享快照；不调度更新、不写运行意图。
- 源码更新调度统一由 reconcile 周期的 `_schedule_updates()` 驱动，依据最新快照的组件
  running/code 事实（忙碌延后、空闲应用、`auto_update=false` 不动、停止意图跳过）；
  观测与调度只经快照传递数据。快照落后一个观测周期（≤3s）属调度语义。
- GUI（R6 定稿）：统一侧栏（服务/项目 + 管理区），设置独立成页、草稿归入添加流程；
  服务行 = 一个主操作 + 稳定日志入口 + ⋯（次要动作收拢）；行可键盘展开分层详情
  （概览/入口/组件/依赖/日志）。域名健康条为一行摘要，完整诊断/安装指引在设置页披露。

## V3.7 可靠性修复 + 可选 5670 域名模块（D1–D4，2026-11-06）

**D1 可靠性（review 2026-11-05 S1/S2/S3/UX1/UX2）**：
- S1 停止优先：`_schedule_updates` 提交更新操作前、以及被排队线程执行前都复核最新
  `Store.intent_read()`；已停止服务绝不被旧快照重启（排队后停止 → 线程放弃并释放 op 槽）。
- S2 resolver 读失败 ≠ 已确认归属：`uninstall` 在内容不可读时拒绝删除（reason=unreadable）。
- S3 观测不写意图：`Store.intent_read()` 只读推导（V2.9 缺省语义不变）；写入口仅
  `intent()`（reconcile/调度惰性播种）与 `set_intent()`。`project_status` 走只读。
- UX1 行键盘展开仅在行自身聚焦时处理；地址 chip 的点击 stopPropagation——Enter/Space
  在 chip 上是复制语义。UX2 `logReading()`（悬停或选区在 `.detail pre` 内）同时门控
  3s 轮询与 `render()` 整树重建，选择/焦点/滚动不被打断。

**D2 网络件退出默认路径**：
- `scripts/install.sh` 不再安装/引导任何 locove 网络件（普通升级不偷偷重装）。
- daemon 的路由收敛经 `locove.gated_sync(settings, ...)`：`use_domains=false`（默认/
  未安装）时不运行任何收敛、不写任何 locove 文件。
- `locove.module_status(settings)` 状态机：`not_installed / installed_off / starting /
  available / failed`（starting=启用后 ENABLE_GRACE 宽限内未就绪）；分层报 dns/proxy/
  resolver 各自原因；`legacy_80` 单独报告旧 80 转发器残留，不参与新数据面健康。
  未安装/未开启不产生 UI 告警。

**D3 可选 5670 按需启用**：
- 业务代理端口：`settings.domain_port`（缺省 **5670**，1024–65535，禁控制面
  47800/47891/5354）；`locove.effective_port()` 是唯一裁决（显式参数 > settings > env >
  缺省）。Caddy 直接监听回环 `:domain_port`（`http_port`），**不再有 80 转发路径**。
- 公开地址统一 `http://<host>:<domain_port><open_path>`：`project_status().endpoints[]
  .public_url` 为单一事实源（UI chip/详情、`lsctl endpoints`、Skill 报告同源）；
  `Project.public_urls(port)` 同步带端口。控制面 47800、DNS 5354、admin 47891 不变。
- `POST /api/locove/enable {port?}` / `disable` / `uninstall {keep_resolver}`（后台线程
  执行，进度并入 `GET /api/locove` 的 `optin` 面）；`localservices/locove_optin.py`：
  preflight（端口占用按 lsof 进程名判定，caddy 缺失、resolver foreign/unreadable 先败）
  → 安装用户件（`install-locove-system.sh --user --port N`）→ 本地验证 → resolver 缺失时
  经 `osascript ... with administrator privileges` 当次授权安装（取消=取消授权）→ 系统
  解析端到端验证 → 才写 `use_domains=true`+`domains_enabled_at`。全新安装被取消/早期
  失败时回滚用户件（零安装副作用）；既有安装不因取消被拆；验证失败不写启用成功。
- 关闭=设置 off + bootout dns/proxy agent（plists/resolver 保留，状态 installed_off）；
  卸载=用户件必删，resolver 经授权可选删（S2 守卫），旧 80 件归 retire 脚本。
- `lsctl locove-enable/locove-disable/locove-uninstall`；`locove-domains --port`；
  `endpoints` 输出模块状态/端口/legacy_80。

**D4 旧 80 迁移**：`scripts/retire-locove-80.sh`（默认 dry-run 盘点；`sudo ... --apply
[--remove-resolver]` 只移除本工具专属命名的 entry plist/libexec/日志目录；resolver
默认保留供 5670 使用；不碰业务进程/数据/登记/他人文件）。缺管理员授权时其余功能
照常，残留如实记录，不伪称 80 已退役。
