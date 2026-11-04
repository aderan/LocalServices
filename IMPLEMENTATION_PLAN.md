# Locove R1–R7 修复与剩余交付（C1–C5）

依据 [Goal](docs/goals/locove-foundation-domains-ui-goal.md) §5 与 [REVIEW-2026-11-04](artifacts/acceptance-LOCOVE-FOUNDATION-DOMAINS-UI/REVIEW-2026-11-04.md)。
逐项修复证据汇总在 [REVIEW-FIXES.md](artifacts/acceptance-LOCOVE-FOUNDATION-DOMAINS-UI/REVIEW-FIXES.md)。

## Stage 1：C1 回环监听与逐组件路由（R1、R2）

Goal：Caddy 站点与 catch-all 全部只绑回环；endpoint 可代理性由目标组件的就绪/归属/实际端口逐个决定。

Success Criteria：
- 生成的 Caddyfile 含全局 `default_bind 127.0.0.1 ::1`，真实 Caddy 进程 `lsof` 只见回环监听，非回环地址不可达。
- partial 服务中 stopped 组件的 endpoint 收敛为 unavailable（503），同服务 ready 组件保持可代理；快照缺组件事实时不猜测（unavailable）；外部实例仍按实际端口转发。
- 未知程序占端口不转发（保持既有语义，新增行为测试）。

Tests：`tests/test_locove_routes.py` 扩展（渲染断言 + 真实进程监听地址 + 非回环拒绝 + partial/多组件/无组件快照）。

Status: In Progress

## Stage 2：C2 安装与定义往返（R3、R4、R5）

Goal：系统文件所有权保护、sudo 全安装身份一致、派生域名声明来源保真。

Success Criteria：
- 安装/卸载对 `/etc/resolver/<suffix>`：已存在且非本工具内容 → 拒绝覆盖/拒绝删除（可审查报错与备份路径）；本工具内容 → 幂等替换/清理。测试用隔离 resolver 路径替身。
- sudo 一次全安装：用户级步骤以目标用户身份/环境执行，状态根与属主为目标用户；系统级步骤独立；文档形态可核验。
- `GET /api/services/<sid>/definition` 的 endpoints 标注 `derived`；注册面把「未编辑的派生入口」继续留在派生态（不写 TOML）；仅当用户显式编辑入口时固化为显式定义。仅改 description 的编辑往返不改变 custom 模式路由资格。

Tests：resolver 替身往返测试、definition/注册往返保真测试、安装脚本静态/沙箱验证。

Status: Not Started

## Stage 3：C3 完整 UI 工作台改造（R6）

Goal：CC Switch（统一导航/页头/当前状态/配置交互）+ Magpie（紧凑行、细分隔、主副文本、行尾对齐、克制状态色）语法的完整工作台；服务详情与设置/草稿/管理区各归其位。

Success Criteria：
- 统一侧栏导航（服务 / 需要处理 / 共享资源 / Agent / 管理）+ 页头；主区为紧凑服务行（名称、当前模式地址、状态、主操作 + ⋯ 收拢次要操作）；选中行展开详情区（概览/入口/组件/依赖/日志/启动配置分层）。
- 设置与草稿分离；已移除管理、恢复流程位置明确；域名诊断收进设置上下文。
- 明暗主题、键盘可达、长文本、断线未知态、编辑期间刷新不打断全部保留或改进。
- 真实浏览器/App 截图前后对照 + 交互证据（`scripts/verify_v2_gui.cjs` 同等断言保留 + 新增）。

Tests：锚点/行为测试 + verify_v2_gui.cjs + 真实 Chrome 截图与 DOM 验证。

Status: Not Started

## Stage 4：C4 观测与调度分离（R7）+ 架构复核

Goal：`_refresh()` 只采集与投影；更新调度统一进 reconcile 周期；等价时序测试先行。

Success Criteria：
- `_refresh` 不再调用 `_tick_updates`；reconcile 基于最新快照驱动更新调度（忙碌延后、空闲重启、auto_update=false 不动）。
- 先补等价行为/时序测试再重构；此前回退的两项 lifecycle 场景（test_4 延后/应用、test_update_policy 全组）与全量测试通过。
- 源码更新、生成器、重试、give-up 后源码修复重试等时序以测试证实仍可用。

Tests：新增观测/调度分离测试；`tests/test_lifecycle.py`、`tests/test_update_policy.py` 全绿；全量回归。

Status: Not Started

## Stage 5：C5 集成验收与修订交付

Goal：R1–R7 逐项闭合记录 + 整体验收。

Success Criteria：
- 全量 `python3 -m pytest tests/ -q` 通过；`node scripts/verify_v2_gui.cjs` 通过；安装/升级/卸载验证；真实无端口访问 + 默认直连/域名开关行为验证；UI 真实截图证据。
- REVIEW-FIXES.md 逐项记录复现/修复/测试/真实证据与提交号；README/CONTRACTS 更新；剩余限制与接续入口明确。

Status: Not Started
