# IMPLEMENTATION_PLAN — Locove／洛湾（LOCOVE-FOUNDATION-DOMAINS-UI）

依据 [Goal](docs/goals/locove-foundation-domains-ui-goal.md) §5 建立本文件；完成后移除。
详细运行与验收记录：`artifacts/acceptance-LOCOVE-FOUNDATION-DOMAINS-UI/`。

环境复核（2026-11-04 执行时）：
- HEAD `2bf3159`（仅交接文档），工作区干净；daemon/menubar 已由 launchd 运行（`com.mybrain.localservices.*`）。
- 80/443 空闲；`/etc/resolver` 不存在；hosts 无 `.locove` 条目；Caddy v2.11.4 在 `/opt/homebrew/bin/caddy`。
- **无免密 sudo**（`sudo -n` 需密码）：`/etc/resolver/locove` 与 80 端口监听需要一次用户授权安装，
  按 Goal §6“未决事项”处理——先交付可审查安装产物，记录精确操作与验证缺口，不伪报完成。
- 基线测试（上一轮实测）：`python3 -m pytest tests/ -q` 436 passed。每次改动后重跑。

---

## Stage 1（M1）— 品牌与核心契约

**Goal**：Locove／洛湾 名称落地全部产品入口；Endpoint/域名数据模型进入 manifest 与注册契约；
意图唯一写入者与依赖顺序这两个已识别的结构性风险先有行为测试再有最小修正。旧 id/launchd 标签/`lsctl` 不变。

**Success Criteria**
1. App 窗口标题、菜单栏、Dashboard 标题/文案显示 Locove／洛湾；`lsctl`、包名、数据目录、launchd label 兼容不动。
2. `manifest.Project` 增加可选 `endpoints[]`（id/host/component/open_path）；无 endpoints 的旧清单按规则派生缺省入口；
   域名 host 冲突在加载/注册/恢复预检中被拒绝。
3. 恢复流程写意图不再绕过唯一写入口；并发 恢复/刷新/启停 不丢意图（行为测试证明）。
4. 组件依赖未知引用与环在加载/注册时明确失败并报告链路（行为测试证明）。
5. `docs/CONTRACTS.md` 增补 V3（Locove 域名/Endpoint）契约；`schemas/register.schema.json` 支持 endpoints。

**Tests**：新增 `tests/test_locove_contracts.py`（意图并发、依赖环/未知引用、域名冲突/派生）；
全量 `python3 -m pytest tests/ -q` 保持绿。

**Status**：🔄 进行中。

## Stage 2（M2）— 无端口完整小路径

**Goal**：一个隔离 Web 服务从登记到 `*.locove` 解析、80 入口、代理转发、启动就绪、无端口打开、停止的闭环组件；
DNS/入口/路由/应用四层健康分别观测；激活失败保持上次有效配置。

**Success Criteria**
1. `localservices/locove.py`：routes.json 单一事实（host→upstream/状态），域名分配与持久化；80 占用/权限不足显示原因。
2. DNS responder（用户级 launchd agent，UDP+TCP 高端口）：已登记名回答 127.0.0.1，AAAA NODATA，未知名 NXDOMAIN；
   不替换全局 DNS。
3. Caddy 承载数据流：daemon 生成 Caddyfile → `caddy validate` → 优雅 reload；失败保留旧文件与旧路由；
   停止的服务返回明确 unavailable 页；移除撤销路由。
4. 80 入口 = 最小特权转发器（LaunchDaemon，root，仅绑定 127.0.0.1:80 转发到用户级代理端口，无 shell、无动态配置）；
   `/etc/resolver/locove` 指向本地 DNS。安装/卸载脚本幂等、只管理自身文件。
5. 无 root 阶段：dig 直连 + curl Host 头验证 DNS/代理/转发器单元行为；系统级 wiring 以可审查产物 + 缺口记录交付。

**Tests**：`tests/test_locove_dns.py`、`tests/test_locove_routes.py`（协议级 DNS 应答、路由生成/校验/回退/收敛）；
`scripts/install-locove-system.sh` / `uninstall-locove-system.sh` shellcheck 级自查 + 干跑输出审查。

**Status**：⬜ 未开始。

## Stage 3（M3）— 通用路由与生命周期

**Goal**：Endpoint 贯穿注册（payload/dry-run diff/覆盖/导出/归档/恢复）、Next 与 Vite 两种样例、真实 Buildbase、
外部实例按实际端口路由、daemon/代理/DNS 重启收敛、安装卸载兼容。

**Success Criteria**
1. 注册 payload `service.endpoints[]` 落盘并在 diff/覆盖（含嵌套 paths）/导出/归档/恢复预检中一致；重名提交前失败；
   显示名修改不破坏域名。
2. 隔离夹具：Next（本机版本）+ Vite 各走完 登记→启动→`<name>.locove` 打开→HMR/WebSocket→停止；
   SSE/长请求/重定向/绝对 URL 行为记录。
3. 真实 Buildbase：`buildbase.locove` 打开 `/zh`（在系统 wiring 就绪后真实验证；此前以 Host 头 + 直连上游验证链路）。
4. 已确认归属且就绪的外部实例路由到实际端口；未知占用不转发；服务停止状态准确；恢复重查域名冲突。
5. `lsctl`/Skill 增补域名面（`lsctl endpoints`/注册带 endpoints；SKILL.md 说明）。

**Tests**：`tests/test_locove_lifecycle.py`（注册→路由→移除→恢复全链路、外部实例、冲突、重启收敛）；
Next/Vite 夹具脚本 + 证据截图/日志。

**Status**：⬜ 未开始。

## Stage 4（M4）— 工作台改造

**Goal**：参考 CC Switch/magpie 的信息架构重做 Dashboard（路由 B：模块化原生 JS），域名地址成为主要信息，
导航、紧凑服务行、详情区、完整登记/编辑/恢复流程保留，轮询不打断编辑。

**Success Criteria**
1. 侧栏导航（服务/需要处理/共享资源/Agent/草稿/已移除/设置）+ 紧凑服务行（名称、无端口地址可点击可复制、
   状态/归属、主操作 + ⋯）+ 选中详情（概览/入口/组件/依赖/日志）。
2. 新增服务向导保留 分析→候选→diff→注册→启动 分步语义；编辑对话框/移除对话框行为不回归。
3. 异步刷新不覆盖新状态、不重建正在编辑的 DOM；键盘/VoiceOver 标签、明暗主题、长名称/域名可用。
4. 品牌一致；`lsbridge`/`lsOpenView` 原生桥保留；`scripts/verify_v2_gui.cjs` 断言迁移后保持同等覆盖。

**Tests**：改造后的 GUI 验证脚本（同等行为断言）+ 真实浏览器（node 无头）交互冒烟 + 截图证据。

**Status**：⬜ 未开始。

## Stage 5（M5）— 集成与接续

**Goal**：全量回归、真实 App/浏览器验证、安装/升级/卸载兼容证据、文档与最终交付。

**Success Criteria**
1. `python3 -m pytest tests/ -q` 全绿；`scripts/verify_v2_gui.cjs` 与真实浏览器验证通过；`menubar/build.sh` 产物可用。
2. `scripts/install.sh`/`uninstall.sh` 含 Locove 系统件（幂等、只管理自身）；升级不丢登记。
3. README/CONTRACTS 更新；`artifacts/acceptance-LOCOVE-FOUNDATION-DOMAINS-UI/` 有命令、证据、阻塞、下一步。
4. 剩余事项（系统级 wiring 的用户授权步骤等）如实列出。

**Tests**：见各验收记录；整体完成以 Goal §6 清单逐条核对。

**Status**：⬜ 未开始。
