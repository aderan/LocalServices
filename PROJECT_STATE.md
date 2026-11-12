# PROJECT_STATE — Locove／洛湾（LocalServices）

快照日期：2026-11-13。维护规则：只记语义状态，不是 README/变更日志；证据分级
[confirmed]（现物直证）/ [documented]（权威文档断言）/ [inferred] / [unknown]。

## 1. 项目身份

本机子项目统一工作台，产品名 **Locove／洛湾**（`LocalServices` 为仓库名与兼容标识）。
组成：菜单栏 + 主窗口 App（AppKit/WKWebView）+ 本地 Dashboard（daemon 自带 Web）+
`lsctl` CLI + Codex Skill。每个子项目从**当前源码目录**启动（不打包副本）。
边界：本地 HTTP 为基础，无云端托管/远程控制面/云同步/遥测/公网 DNS/HTTPS/443。

## 2. 当前目标

D1–D5（LOCOVE-FOUNDATION-DOMAINS-UI）2026-11-06 交付；W1–W4
（LOCOVE-WORKBENCH-UI-REFACTOR）2026-11-12/13 交付，验收证据
`artifacts/acceptance-LOCOVE-WORKBENCH-UI/`（提交 1871364 → 5e84af6 → 87d384c → W4 收尾）：

- W1 共享工作台：960 最小窗布局（四列服务行/设置容器查询单列）+ 轮询稳定性
  （未变跳过重建/焦点/滚动/展开态保持）+ data-act 稳定标识 + #p-<id> 深链展开。
- W2 故障路径：九态夹具（隔离 daemon 真实 failed/conflict/available + 注入）；
  重试/日志真实写路径；lsDiagnose 原生与浏览器同路径；gave_up 慢周期自愈落地。
- W3 菜单栏：失败服务跳诊断；**修复菜单打开期间 5s 刷新整树重建销毁会话的缺陷**
  （也是既有 verify_menubar_clicks.swift 不稳根因）；System Events 原生冒烟 + 实机
  install.sh 重发布 launchd 验收通过（上轮 Launch Constraint Violation 未复现，
  根因是绕过 bootout 直换二进制的替换路径）。
- W4 集成：pytest 587+14 全绿；D1/R6/W1/W2 浏览器回归与原生冒烟全绿。

## 3. 当前状态

已确立：

- 默认**本地直连**；域名是按需启用的可选模块（`:5670` 高端口，用户级 DNS 127.0.0.1:5354
  + Caddy 回环数据面；启用需当次管理员授权装 `/etc/resolver/locove` split DNS，取消零副作用）。
- 旧 80 无端口入口已在实机退役为迁移项（`scripts/retire-locove-80.sh`）。
- V1/V2 已交付；接口契约冻结于 `docs/CONTRACTS.md`（PROJECTS-V1 / LIFECYCLE-V2）。
- 测试：pytest 587 项 + 无头 GUI/浏览器验证脚本（`scripts/verify_v2_gui.cjs`、
  `verify_w1/w2_browser.cjs`、`verify_w3_native.sh`、`verify_d1/r6_browser.cjs` 等）。
- 工作树无在途未提交改动（gave_up 慢周期自愈已随 W2 提交并补测试）。

实机运行（2026-11-13 安装后）[confirmed]：daemon 与 menubar App 均由 launchd 运行
（install.sh 重发布，无 LCV）；11 个服务 browsercapture/explore/englishlearn-admin/
shicha 可用，mediascope partial，orx-analytics 引擎无响应，其余 stopped。

未确立：独立「启动 Colima」按钮（待范围确认，Goal §5）；菜单栏 popover 面板形态
（本轮保留 NSMenu，可选迭代）。

已知限制：WKWebView 的 AX 文本桥在菜单自动化会话后不可读（W3 原生冒烟中
DIAGAX=0，窗口在屏；页面内容级验证经浏览器证据覆盖同一 lsDiagnose）。

## 4. 结构

- `localservices/`：daemon（仅监听 127.0.0.1:47800，launchd KeepAlive）、controller、
  registry、adapters（MediaScope/BrowserCapture/To2050 忙碌判断，orx-analytics 只读）、
  `web/dashboard.html`（单文件 Dashboard）。
- `menubar/`：菜单栏 + 主窗口 App（`~/Applications/MyBrain Services.app`）。
- `services/*.toml`：随仓内置清单（shicha/buildbase/climbo/explore/ui-atlas/
  mediascope/to2050/browsercapture/folklio/orx-analytics）；用户注册写入 user_services_dir，
  tombstone 在用户配置层 `removed.json`（内置模板永不修改）。
- `scripts/`：install/uninstall（幂等）、locove 域名件、retire-locove-80、verify_* 验证脚本。
- `tests/`：430 项 + `fixtures/`（vite/fastapi/go/next/compose 代表工程）。
- `docs/`：`CONTRACTS.md`（接口唯一权威）、`goals/`（每轮 Goal 文档）、`research/`。
- `artifacts/`：`acceptance-*`（逐 AC 验收证据，flb 的验收标准要求证据归档）、`incidents/`。

治理：仓库无 AGENTS.md/CLAUDE.md；每轮以 `docs/goals/<goal>.md` 为权威边界 +
`artifacts/acceptance-*` 为验收证据。MyBrain/Products 下无本产品目录（无外部认知层）。

## 5. 关键决策（勿无故重开）

- 域名能力**可选、按需**：默认安装不装任何 DNS/Caddy/resolver；所有监听仅回环；
  绝不保存管理员密码；关闭=回直连（安装件保留）。早期"80 无端口特权入口"已被
  高端口方案替代，不以旧书签兼容为由保留。
- 移除只解除登记，**永不删除**源码/业务数据/共享容器/卷；内置模板用 tombstone 遮蔽。
- 受管失败治理：launchd 包装 runner，短命退出指数退避，连续 5 次停止自动恢复；
  源码变化重试一次（本周期新增慢周期重试，见 §3）。从不停止未知归属进程；
  端口被他人占用显示冲突、不可接管。
- Compose 语义：up/start/stop 分开，绝不 down/rm/删卷；`provision="up"` 须显式
  `confirmed_provision=true`。
- UI 参考锚点：farion1231/cc-switch（导航语法）+ yetone/magpie（紧凑行）；R6 已重组。
- 技术栈保持 Python + launchd + AppKit/WKWebView + Caddy；不以迁移 React/Tauri 为前提。

## 6. 在途工作

- 无（W1–W4 已交付归档；下一轮方向见 §8）。

## 7. 约束与风险

- 系统代理 Clash Verge 会用自家模板重写代理绕行表，可能覆盖 `*.locove` → 浏览器
  "无法访问"（链路本身正常）；临时修 `scripts/locove-proxy-bypass.sh`，永久修在
  Clash Verge 设置加 `*.locove`。`lsctl endpoints` 如实显示该层。
- Vite 类 dev server 需项目显式 `server.allowedHosts: ["xxx.locove"]`（勿设 true）。
- 验收纪律：失败=无变化；证据必须归档到 `artifacts/`；独立审查线可重开 Delivered。
- 实机异常待处理 [confirmed]：orx-analytics failed、mediascope partial——属本轮
  范围外，接手时先看诊断再决定是否开新线。
- 测试与 launchd 纪律：生命周期测试用隔离 label 前缀与临时目录，不碰真实实例；
  模块导入期不得改 env（污染并行测试）。

## 8. 下一步方向（非承诺）

- 独立「启动 Colima」按钮：待用户确认范围（Goal §5 的约束：不隐含远程 Docker/重启
  其他容器/磁盘解锁）。
- 菜单栏 popover 面板形态（Stitch menubar 稿）可选迭代；NSMenu 已满足键盘可达验收。
- orx-analytics 失败原因与 mediascope partial 是否开线，待用户裁定。
