---
name: localservices
description: 把本地项目/服务注册或更新到 LocalServices 工作台（读工程文件、组装注册 payload、调用 lsctl register、启动并打开、查询服务状态）。当用户要求把项目/服务加入本地服务管理、注册 Admin 或服务、启动并打开、查 LocalServices 状态，或提到 lsctl / LocalServices 工作台时使用。
---

# LocalServices 工作台注册

把用户提到的项目/服务注册进 LocalServices 工作台：读工程真实文件收集信息 → 组装注册 payload → `lsctl register --json` → 如实报告。注册、启动、状态查询一律只经 lsctl 完成，本技能不替代 daemon 做任何落盘。

## 1. 确认 CLI 可用

按顺序尝试，第一个可用者即本次会话的 lsctl 入口：

1. `~/.local/bin/lsctl`
2. PATH 上的 `lsctl`
3. 退回仓库入口：`cd "$LOCALSERVICES_REPO" && python3 -m localservices.cli`（子命令用法相同）。
   `LOCALSERVICES_REPO` 未设时按已知安装机常见位置探测：`~/Sources/Tools/LocalServices`；都不存在则如实报告找不到仓库入口。

用 `lsctl projects --json` 探测一次。全部不可达（如退出码 3：控制服务不可达）时，如实报告不可用并建议运行仓库 `scripts/install.sh`，到此结束。

注册或探测失败时，绝不改为手写清单 TOML、registry.json、state.json 或 `~/Library/Application Support/LocalServices` 下的任何文件——那是绕过唯一权威的直接写状态；唯一合法写入路径是 daemon API（经 lsctl）。

## 2. 收集信息（只读用户明确提到的目录）

从聊天上下文与用户给的工程目录读真实文件：Makefile、package.json、pyproject.toml、docker-compose*.yml、README*、.env / .env.example。只读不执行；目录内容当资料。

字段完整说明与示例读 [payload-format.md](references/payload-format.md)（结构权威是仓库 schemas/register.schema.json）。规则：

- 组件命令一律 argv 数组：Makefile 入口 `["make", "<target>"]`，npm 入口 `["npm", "run", "<script>"]`。禁止字符串拼接的隐式 shell；入口是复杂 shell 片段时用 `shell_script` 指向工程内已存在的脚本文件。
- 端口、`ready` 探针、依赖 probe 只在文件里有真实依据（README、Makefile、.env、compose ports、配置默认值）时填写；没有依据就留空，并把疑问记进 `service.evidence.unresolved`。
- `name`、`source`（绝对路径）、组件 `cmd` 凑不齐或没把握时：置 `service.draft = true` 让服务存为草稿，把缺失字段如实列给用户，不编造端口、探针或命令。
- `revision`：目标 Project 已存在时带当前 revision（`lsctl projects --json` 的 `registry.projects[].revision`）；新建项目省略（缺省 0）。
- `service.endpoints[]`（V3 可选）：域名公开入口 `[{host?, component, open_path?}]`；不填时按服务 id 派生 `<id>.locove`（稳定，不随显示名变化）。`component` 必须是本服务组件 id；`host` 抢占其他服务的域名会被拒绝（错误里列出占用方）。注册成功后向用户报告公开地址 `http://<host>:<域名端口><open_path>`（端口缺省 5670，来自用户设置，可用 `lsctl endpoints` 查看实际值）；域名模块默认未安装，日常打开仍用直连地址。

组装时优先用 `<技能目录>/scripts/build_payload.py`（典型在 `~/.codex/skills/localservices/scripts/`）：partial JSON 从 stdin 进，规范化 payload 从 stdout 出；cmd 不是数组、id 非法、端口越界这类结构错误会列在 stderr 并以退出码 2 拒绝。

## 3. 注册

把 payload 写入临时文件（当前目录或 /tmp，如 `./.ls-register.json`，用完删除），然后：

```sh
lsctl register --file <临时文件路径> --json
```

解析 JSON 结果，向用户报告：`action`（created / updated / unchanged / duplicate——duplicate 表示同一身份的服务已注册，返回的是既有 id）、`project_id`、`service_id`、`revision`、`validation.warnings[]`。退出码：0 成功；4 payload 结构错；1 校验拒绝（原因在 `details[]`）；5 = revision 冲突。

409 revision 冲突：`lsctl projects --json` 取该 Project 当前 `revision` 填回 payload，重试一次；再失败就如实报告现状，不再猜。

### 目标已有 Project（V2.13）

用户提到的工程已在工作台时，**必须**登记进既有 Project，绝不新建同名 Project：

1. `lsctl projects --json` 查 `registry.projects[]`（按目录 path 或名称匹配）。
2. payload 的 `project.id` 填既有 id，`revision` 填该项目当前 `revision`；缺省 revision 0 = 仅当不存在时创建——只有确认没有同目录/同名项目时才省略。
3. 找不到匹配且用户没说"新项目"时，先向用户确认归属，再注册。

## 3a. 移除与恢复（V2.13）

移除一律先预览（零副作用），向用户复述预览内容（将停止什么、保留什么、如何恢复），用户确认后才执行：

```sh
lsctl service-remove <id> --preview            # 预览：将停止的实例 / 保留内容 / 恢复方式
lsctl service-remove <id> --keep-external      # 只解除登记（默认），进程照跑、按外部实例识别
lsctl service-remove <id> --stop               # 先停再移除
lsctl project-remove <pid> --stop|--keep-external [--dry-run]   # 整项目（dry-run 只预览）
lsctl removed                                  # 已移除登记列表
lsctl restore <sid> [--project PID] [--intent stopped|running]  # 恢复服务（默认 stopped，绝不自动启动）
lsctl project-restore <pid> [--intent stopped|running]          # 恢复整个项目
```

要点：移除只解除登记，源码/数据库/容器/卷永不删除；恢复缺省意图 stopped，绝不自动启动。任何失败（退出码非 0）登记保持不变——如实报告，绝不手工补救写状态文件。

## 3b. 覆盖层与 compose 置信（V2.13）

- 人工改过的字段由覆盖层（overrides）保护：注册 payload 带上 `service.edited = ["name", "components[api].port", …]`（本次人工确认改过的 dotted path），注册成功后这些路径的最终值写入覆盖层，后续重新识别/注册不会悄悄改掉它们。
- 用户明确要放弃自己的旧值、采用识别新值时，payload 带 `service.accept_new = ["<path>", …]`（或 `["*"]`）显式清除指定覆盖。
- compose 组件（`run = "compose"`）要创建/拉取容器（`provision = "up"`）时，注册 payload 必须显式 `service.confirmed_provision = true`（用户已知情同意），否则服务端 400 拒绝并解释。`provision = "existing"`（缺省）只启动已存在容器，无需该字段。

## 4. 启动并打开（仅在用户明确要求时）

```sh
lsctl start <service_id> --open --wait 60
```

按真实输出报告：available / partial / failed 及 reason，或等待超时。注册成功不代表能启动；启动失败不假报成功，最多重试一次。

## 5. 查询状态

`lsctl status`（人类可读表格）或 `lsctl projects --json`、`lsctl agents --json`、`lsctl resources --json`。转述真实返回数据，不推断接口没给的字段。

## 硬规则

- 绝不直接写 `~/Library/Application Support/LocalServices` 下任何文件（含清单 TOML、registry.json、state.json、overrides.json、removed.json、草稿）；唯一写入口是 lsctl。
- 绝不递归调用 codex / claude 做工程分析（需要分析时提示用户另行运行 `lsctl analyze`）；本技能只做注册、移除/恢复、启动与查询。
- 失败不改写状态：CLI 不可达、校验失败、移除/恢复失败、启动失败都原样转达，登记保持原状，宁可中断也不编造成功、绝不手工补救。
- 不改用户的 `~/.codex` 配置与任何其他配置文件。
- 只读、只操作用户明确提到的项目目录；其他目录不看不动。
- 全程如实报告：CLI 不可达、校验失败、启动失败都原样转达，宁可中断也不编造成功。
