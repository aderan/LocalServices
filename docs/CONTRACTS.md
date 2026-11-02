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

## V2.3 覆盖层（人工覆盖）

- `overrides.json`：`{"schemaVersion":1,"services":{<sid>:{"fields":{<dotted path>:value},
  "updated_at"}}}`；路径形如 `name`、`description`、`open`、`components[api].port`。
- 注册 payload `service.edited: [paths]`（GUI 在候选审核时 diff 用户编辑并携带）→ 注册成功
  （created/updated）后这些路径的**最终值**写入覆盖层。
- 注册合并：进入比较/写盘前，先把 `overrides[sid].fields` 应用到新定义（覆盖层优先）；payload
  `service.accept_new: [paths]`（或 `["*"]`）显式清除指定覆盖。**dry_run diff 在应用覆盖后计算**，
  预览=实际。
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
