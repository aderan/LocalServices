# LocalServices

本机子项目的统一启停与状态：菜单栏 App + 本地 Dashboard + `lsctl` CLI。
每个子项目从**当前源码目录**启动（不打包副本），源码变化按策略自动生效。

## 安装 / 更新 / 卸载

```sh
scripts/install.sh            # 幂等：构建菜单栏 App、安装控制服务与 lsctl
scripts/uninstall.sh          # 停止并移除本工具的全部 LaunchAgent、App、CLI
scripts/uninstall.sh --purge  # 另删本工具状态与日志；子项目数据始终不动
```

安装后：

- 控制服务 `com.mybrain.localservices.daemon`（launchd KeepAlive），仅监听 `127.0.0.1:47800`；
- 菜单栏 `~/Applications/MyBrain Services.app`（登录自启，退出后不自动拉起，后台服务继续运行）；
- Dashboard：菜单栏「打开 Dashboard」，或 `lsctl open`；
- CLI：`~/.local/bin/lsctl status|start|stop|restart|open|logs <project> [--json] [--open]`。

## 模型

- `services/<id>.toml` 是项目清单：组件（server / worker / generator）、命令、端口、就绪探针、
  依赖、热更新与监视范围。端口固定写在清单里，用户不需要管理地址。
  清单加载时拒绝重复监听端口；运行时其他受管项目占用端口也会报告冲突。
- 每次启动共用 30 秒预算（包括安装、编译、生成和就绪探测）；进程提前退出立即报错。
  超时后清理本轮拉起的组件，保留错误并释放操作状态；可以停止或重试，不自动循环启动。
  Next.js 首次冷编译若超过预算，同样显示失败；不会把仅端口可连接当作页面已就绪。
- 必要依赖先检查，未就绪前不编译或启动 API。Climbo/ShiCha 可自动恢复已有 Colima default
  及清单登记的原 PostgreSQL/Redis 容器；独立显示「准备环境」（最多 120 秒），环境就绪后
  才开始应用的 30 秒预算。共享恢复串行执行；容器不存在、数据卷或端口映射不符时明确拒绝。
  不创建数据库/容器、不拉镜像、不切换用户的 Docker 上下文；停止项目不停止容器或 Colima。
- 每个组件一个 LaunchAgent `com.mybrain.ls.<project>.<component>`，由 runner 包装：
  短命退出计失败，指数退避（最多 60s），连续 5 次后停止自动恢复；源码再变化时自动重试一次。
- 运行意图持久化在 `~/Library/Application Support/LocalServices/state.json`；
  控制服务重启或重新登录后按意图恢复，并重新发现已在运行的实例。
- 外部实例：从项目自己目录启动且通过就绪检查的实例（IDE / agent 会话 / 终端起的）显示「外部运行」，
  可直接打开（按其实际端口）。点「接管」（`lsctl takeover <project>`）才会停止该实例及其启动包装进程，
  改由本工具从最新源码启动；项目忙时延后到空闲自动接管。端口被**其他程序**占用时显示「冲突」，
  不可接管，**从不停止**。
- 源码更新：
  - `hot` 组件依赖 dev server 热更新（Vite / Next）；FSEvents 失效时自动降级为变化即重启；
  - 其他组件变化后重启；有 `build` 的（Go）先构建，失败保留旧实例并显示「更新失败」；
  - 项目忙（适配器报告任务 / 录制进行中）时延后更新，不打断任务；
  - generator 组件在输入变化后重新生成，失败保留上次有效输出。
- 适配器（`localservices/adapters/`）：MediaScope、BrowserCapture、To2050 的忙碌/健康判断，
  orx-analytics 仅只读观察。
- Climbo 模板编辑（`services/climbo.toml`）：从当前源码起管理端 `127.0.0.1:3001/admin/templates`
  与本地 API `:8080`，数据库指向已有的 `climbo-postgres :5434`。Postgres/Redis 可按需恢复，
  Metadata 只观察；停止项目均不停止这些依赖，也不连接生产管理端。
- 日志 `~/Library/Logs/LocalServices/`（0600，轮转，令牌与凭据脱敏）。

## 测试

```sh
python3 -m unittest discover -s tests -v   # 生命周期测试：临时目录 + 合成服务，隔离 label 前缀
python3 tests/verify_next_clone.py         # Next.js 热更新：在 Buildbase 的 APFS 克隆上验证
```

测试不触碰真实项目实例与数据。

### ShiCha 开发启动契约

ShiCha 的受管 API 和终端 `make api-run` 共用其仓内 `scripts/dev_api.py`：构建 API/commerce-sync，生成与 API 端口一致的接收配置，再启动。就绪探针要求实时 `/v1/admin/commerce/runtime` 的 `receiverEnabled=true`，避免仅数据库可用却没有接收采集的实例被标为就绪。crawler/acquire/采集契约变化也触发重建。管理端代理明确指向 API :8000。

排查配置漂移或“本地保存但未进入整理”：在 ShiCha 仓运行 `make api-dev-doctor`。它核对实际进程配置并检查待接收回执；非零退出须查看 ShiCha API 的 `[commerce-auto]` 日志。旧 Application Support 下手工维护的 receive.json 不再是受管启动配置源。

### Explore 采集与灵感库

- 菜单栏／Dashboard 独立入口为 **Explore**，`lsctl start explore --open`，打开 `http://127.0.0.1:5178/explore/#/capture?mode=agent`。
- 前端从 UI-Atlas 的 `apps/explore` 运行，后台为 `tools/explore-server/src/server.mjs`（8787）；后台先就绪，再启动前端。复用项目 `.env`／`.env.local` 与既有资料目录，不另建空库、不自动开始采集。
- 后台设置 `auto_update = false`，代码更新后在任务结束时手动重启，避免采集过程中因源码变化自动中断。前端继续热更新。
- Explore 与原 UI-Atlas（5175）保留各自进程，由「UI-Atlas 工作区」统一展示。Atlas 的 `/explore` 是代理入口，需要 Explore 服务也运行；直接打开 Explore 不依赖 Atlas。
- 两者都使用 Vite 且同仓库，声明 `external_cwd_exact = true`，按各自工作目录识别外部实例，防止将 Explore 当作 Atlas 打开或接管。其他清单默认保留子目录匹配。
- 已从终端／Agent 启动的 Explore 显示「外部运行」并可打开；登记入口不意味着已接管，也不会停止原实例。

### UI-Atlas 工作区的操作范围

菜单栏和 Dashboard 将相关服务归在一个工作区，仍保留原服务的状态、日志和运行意图：

| 操作 | 图鉴 5175 | Explore 前端 5178 | API 8787 |
|---|---|---|---|
| 重启全部（3 个服务） | 重启 | 重启 | 重启 |
| 重启图鉴 | 重启 | 保持 | 保持 |
| 重启 Explore（前端＋API） | 保持 | 重启 | 重启 |
| Explore 详情：仅重启前端 | 保持 | 重启 | 保持 |
| Explore 详情：仅重启 API | 保持 | 保持 | 重启 |

工作区启动／重启先处理 Explore（API→前端），再处理图鉴；停止顺序相反。某一步失败即停止后续操作，保留具体错误，不显示虚假的整体成功。运行中采集或不可读的任务响应会阻止重启；暂停和历史排队记录不被误当运行任务。外部实例须先显式接管，工作区操作不会直接停止未知归属进程。

```sh
lsctl restart-group ui-atlas           # 工作区三个服务
lsctl restart explore                 # Explore 前端＋API
lsctl restart explore --component api # 仅 API
lsctl restart explore --component web # 仅 Explore 前端
lsctl restart ui-atlas                # 仅图鉴
lsctl start-group ui-atlas
lsctl stop-group ui-atlas
```

组件列表由后台验证，空列表、未知组件和重复组件均拒绝，不能退化成全量重启。Explore 的任务历史读取采用单独的有界 8MB 上限，避免通用健康探针的 256KB 上限截断 JSON、误报“任务状态不可读”；超限仍拒绝操作。
