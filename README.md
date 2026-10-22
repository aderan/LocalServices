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
