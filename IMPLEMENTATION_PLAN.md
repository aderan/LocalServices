# IMPLEMENTATION_PLAN — Locove 可靠工作台 + 可选 5670 域名（D1–D5）

依据：docs/goals/locove-foundation-domains-ui-goal.md（§4 设计/流程、§5 D1–D5、§6 验证）；
review S1/S2/S3/UX1/UX2 见 artifacts/acceptance-LOCOVE-FOUNDATION-DOMAINS-UI/REVIEW-2026-11-05.md。
基线：e9b454e（工作区干净）。本文件完成后移除。

状态图例：Not Started / In Progress / Complete。

## Stage D1 — 核心可靠性（S1/S2/S3/UX1/UX2）

Goal：停止决策优先于旧快照调度；观测不写意图；resolver 读失败不删；复制/日志键盘与阅读不被打断。

Success Criteria：
- S1：旧快照 intent=running 且当前 Store 已 stopped（ops 已清）时，reconcile/_schedule_updates 不再排队重启；行为测试复现交错时序。
- S2：resolver uninstall 在内容不可读时拒绝删除（reason=unreadable），文件保持。
- S3：fresh Controller 单次 refresh_snapshot() 后 state.json 不出现（观测不落意图）；reconcile/set_intent 仍正常初始化。
- UX1：聚焦地址 chip 按 Enter/Space 触发复制而非展开；点击 chip 不再冒泡触发行展开。
- UX2：悬停/选中日志文本时 3s 轮询不重建内容区；选择与滚动保留。

Tests：tests/test_d1_reliability.py（S1/S2/S3 行为先行）；scripts/verify_d1_browser.cjs（UX1/UX2 无头浏览器断言）。

Status: In Progress

## Stage D2 — 网络件退出默认安装与运行

Goal：普通安装/升级/运行不依赖、不启动 DNS/Caddy/路由 sync；控制面独立；旧声明保真；未安装域名不算故障。

Success Criteria：
- install.sh 不再安装/引导任何 locove 网络件（打印可选能力提示）。
- daemon 的 locove sync 循环仅在域名模块开启时收敛；关闭时 bootout DNS/代理 agent，循环空转不写文件。
- /api/locove 健康面区分 未安装/已装未开/开启 各态；use_domains=false 时不出现域名告警条。
- 既有 endpoints 声明、TOML 读取、注册/恢复不回归。

Tests：tests/test_d2_no_default_network.py；现有 locove/registry 套件保持绿。

Status: Not Started

## Stage D3 — 可选 5670 按需启用（预览/授权/状态机/统一 URL）

Goal：设置页预览实际 URL（http://<name>.locove:5670/...）→ 确认 → 预检（端口/依赖/归属）→ 用户级安装 → 需要时经真实授权机制（osascript 管理员当次授权）装 .locove resolver → 验证 → 才标成功；取消零副作用回直连；关闭/卸载状态如实。

Success Criteria：
- 公开 URL 统一含 :5670（默认；domain_port 可配、冲突预检 bind 测试、URL/设置/agent env 一致，不静默换端口）。
- Caddy 直接监听回环 5670（default_bind 回环保留），不再经 80 转发；DNS 5354、控制面 47800 不变。
- enable 流程分步可见（预检/用户件/resolver 授权/验证），授权取消回滚用户件，状态回 未安装；成功后 state=available 且 use_domains=true。
- 端口被占、caddy 缺失、resolver 他人所有、验证失败均报具体原因；失败不写 use_domains=true。
- 关闭：use_domains=false + 停止 DNS/代理 agent + sync 空转；resolver 保留如实展示；卸载：当次授权删 resolver+用户件，S2 保护生效。
- UI/CLI/Skill/契约的 public_url/说明同步 :5670。

Tests：tests/test_d3_optin_5670.py（状态机/预检/回滚/URL 统一/设置校验）；隔离 daemon 实测 enable 各失败路径。

Status: Not Started

## Stage D4 — 旧 80 系统件迁移

Goal：归属盘点 + 可审查迁移产物（scripts/retire-locove-80.sh：dry-run 默认、幂等、只动确认归属件）；业务切 5670 验证；缺权限只阻塞实机验证并如实记录。

Success Criteria：
- 盘点输出：80 entry LaunchDaemon/libexec、resolver、用户 agents、代理绕行归属；保留/移除清单明确。
- 迁移后 80 不再由 Locove 占用（其他进程占用则单独报告）；业务进程/数据/登记不动。
- resolver 按 --keep-resolver（默认，5670 仍需解析）/--remove-resolver 处理；代理绕行只回收可证明本工具增加的条目。
- 实机执行需 sudo/osascript 授权；无法授权时记录残留，不伪称退役。

Tests：tests/test_d4_migration.py（脚本 dry-run 输出、归属守卫、幂等）；实机迁移证据入 artifacts。

Status: Not Started

## Stage D5 — 集成与产品验收

Goal：全量回归 + App/浏览器证据 + 文档/契约/README/Skill 一致 + 证据归档。

Success Criteria：
- python3 -m pytest tests/ -q 全绿；verify_v2_gui / verify_r6_browser / verify_d1_browser 通过。
- 真机：默认直连全功能（查询/打开/复制/日志/启停）在无网络件下可用；启用 5670 后 buildbase.locove:5670 页面/资源/HMR 实测；关闭回直连；迁移后 80 退役。
- docs/CONTRACTS.md V3.7、README、SKILL.md、CLI 帮助更新；旧无端口说明改为 5670。
- artifacts/acceptance-LOCOVE-FOUNDATION-DOMAINS-UI/ 新增本轮记录：D1–D5 状态、S*/UX* 闭合证据、实际 URL、权限/残留、剩余限制。

Status: Not Started
