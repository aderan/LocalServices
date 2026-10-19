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
- 日志 `~/Library/Logs/LocalServices/`（0600，轮转，令牌与凭据脱敏）。

## 测试

```sh
python3 -m unittest discover -s tests -v   # 生命周期测试：临时目录 + 合成服务，隔离 label 前缀
python3 tests/verify_next_clone.py         # Next.js 热更新：在 Buildbase 的 APFS 克隆上验证
```

测试不触碰真实项目实例与数据。
