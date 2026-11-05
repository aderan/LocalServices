# 本地域名与 UI 开源参考核查

日期：2026-11-04。范围：外部一手资料、参考项目源代码与官方截图；不改运行逻辑。Magpie 已由用户确认为 `yetone/magpie`。

需求更新：用户要求浏览器地址不出现端口号，并希望服务域名为「服务名.工具品牌」两段形式；2026-11-04 已确认品牌为 **Locove／洛湾**。§8 对应最新约束；前文 `.localhost` 是初始方案与已有实测，不作为更新后的默认建议。完整交付范围以 [实施 Goal](../goals/locove-foundation-domains-ui-goal.md) 为准。

2026-11-05 接续说明：之后的权限讨论转为默认直连、域名可选且按需安装，采用 5670 高端口实施假设，80 不再是基础交付。当前范围以 Goal D1–D5 为准；本文关于无端口 80 的说明保留为原始方案及规范事实，不代表仍要求安装低端口入口。

## 1. 结论

- `http://buildbase.localhost` 可实现。域名解析与端口路由是两件事：浏览器把域名解析到回环地址，不会自动知道应用正在监听的 3000 等端口。无端口 HTTP 访问需要本地 80 端口入口，再按 Host 转发；HTTPS 对应 443。[RFC 6761 §6.3](https://www.rfc-editor.org/rfc/rfc6761.html#section-6.3)、[RFC 9110 §4.2](https://www.rfc-editor.org/rfc/rfc9110.html#name-http-uri-scheme)
- 参考 CC Switch 与 Magpie 不要求迁移桌面技术栈。它们的价值主要在页面组织、信息密度、状态与操作的关系；LocalServices 已有 AppKit + WKWebView，可保留壳与 daemon，改造 Web UI。
- 建议将桌面主窗口作为完整工作台，菜单栏作为收藏服务/异常服务的快捷入口。主窗口采用 CC Switch 当前源码的稳定侧栏与页头组织，服务行采用 Magpie 的紧凑列表与行内操作；不照搬两个产品各自的业务分类。

## 2. `.localhost` 的行为和兼容性

RFC 6761 对 `localhost` 及其所有子域规定回环语义，地址解析库 SHOULD 直接返回回环地址；这是规范要求，不能据此推断所有实际客户端已经实现。Microsoft 官方兼容性文档指出，现代常青浏览器通常自动支持 `*.localhost`，但 Safari/macOS 可能不支持自动解析，其他原生客户端也可能使用普通系统 DNS。[RFC 6761 §6.3](https://www.rfc-editor.org/rfc/rfc6761.html#section-6.3)、[Microsoft 官方兼容性说明](https://learn.microsoft.com/en-us/aspnet/core/test/localhost-tld?view=aspnetcore-10.0#browser-compatibility)

本机证据（由主分析者实测并提供）：macOS 26.6.2、uid 501，系统 `getaddrinfo(buildbase.localhost)` 返回 `127.0.0.1` 和 `::1`；Codex 内置浏览器实际访问 `http://buildbase.localhost:3000/zh` 成功显示 Buildbase 首页。此证据说明这台机器及被测浏览器带端口访问已可行，不能外推到全部 Safari 版本或其他机器。

同次本机探测：普通用户直接 `bind(127.0.0.1:80)` 和 `bind(127.0.0.1:443)` 均得到 `PermissionError: [Errno 13] Permission denied`，探测未 listen 且 socket 随即关闭。因此本机无端口方案还需解决监听权限/系统入口配置，不能只改 URL；这是本机结果，不能断言所有 macOS 部署方式都必须使用相同提权机制。

### 推荐接入形态

```text
浏览器 buildbase.localhost
  → 回环 80/443 的统一入口
  → 精确 Host 路由 buildbase.localhost
  → 对应服务的实际 loopback endpoint
  → 应用自身已有的 /api 代理或后端调用

LocalServices daemon
  → 从登记定义与已验证运行实例生成路由
  → 验证配置后提交给代理
```

建议先实现明确登记的域名，不将 Host 任意内容拼进 upstream；代理仅回环监听。系统解析不覆盖的客户端可提供按登记生成 hosts 条目的兼容选项，或独立本地 resolver；hosts 不应被当作通配域名方案。这里的兼容路径是设计建议，尚未在本机安装或验证。

代理建议优先验证 Caddy，避免在 Python 控制 HTTP server 内自行补齐通用代理协议。Caddy 官方 `reverse_proxy` 支持 WebSocket 升级与 SSE/流式转发，默认保留 HTTP upstream 的 Host，并处理转发头；配置 reload 对长连接存在明确语义，需要测 HMR 重连，不能只测首页。[Caddy reverse_proxy 官方文档](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#streaming)

HTTPS 应独立增量：本地域名不会自动得到受浏览器信任的证书，Caddy 本地 CA 的根证书需要被客户端信任。`.localhost` 的 secure-context 特殊处理也不等于 HTTPS 信任链已建立。[Caddy Automatic HTTPS](https://caddyserver.com/docs/automatic-https#local-https)、[W3C Secure Contexts §3.1](https://www.w3.org/TR/secure-contexts/#is-origin-trustworthy)

### 框架与验收关注点

| 事项 | 核查结论 / 应测行为 |
|---|---|
| Vite Host | 当前 Vite 默认允许 localhost、`.localhost` 子域与 IP；不需要为此设 `allowedHosts: true`。项目实际版本仍须核对。 |
| HMR | 反代必须转发 WebSocket；若自动推导 HMR URL 不对，再显式设置 host/clientPort/protocol，不预先给所有项目注入固定参数。 |
| Next.js | 当前官方存在 `allowedDevOrigins`；应按项目 Next 版本检查具体支持和默认策略，不直接套最新配置到 Next 15。 |
| Origin / CORS | 域名、scheme 或端口变化会影响请求来源；验证应用调用 API 的实际路径与授权配置。 |
| Cookie / 登录 | 检查 host-only cookie、已有 Domain 设置、Secure、回调白名单与绝对 URL；保留直接端口访问作为回退。 |
| 生命周期 | 域名重复、未知 Host、服务停止/移除、外部实例切换、端口改变、代理重启、配置提交失败均须行为验证。 |
| IPv4 / IPv6 | 本机解析返回两种回环地址，入口只监听一种时应测试客户端回退；不能只测 curl IPv4。 |

框架事实来源：[Vite server.allowedHosts / server.hmr](https://vite.dev/config/server-options)、[Next allowedDevOrigins](https://nextjs.org/docs/app/api-reference/config/next-config-js/allowedDevOrigins)。其余条目是待验收项，不表示已确认现有项目存在问题。

## 3. CC Switch：哪些值得借鉴

核查版本：`farion1231/cc-switch` main commit `51fce968bca807aeee93be9420c651afb582c2c8`。

- 实际技术栈为 Tauri 2 + Rust 后端、React 18 + Radix UI + Tailwind + TanStack Query 等前端库；不是 SwiftUI。[固定版本 package.json](https://github.com/farion1231/cc-switch/blob/51fce968bca807aeee93be9420c651afb582c2c8/package.json)
- 当前 `Sidebar.tsx` 将应用目录、全局功能和设置组织在可收起侧栏，设置态切为设置目录；`AppPageHeader` 统一页面标题、说明和右侧动作，并考虑桌面窗口拖动区域。[Sidebar](https://github.com/farion1231/cc-switch/blob/51fce968bca807aeee93be9420c651afb582c2c8/src/components/shell/Sidebar.tsx)、[AppPageHeader](https://github.com/farion1231/cc-switch/blob/51fce968bca807aeee93be9420c651afb582c2c8/src/components/shell/AppPageHeader.tsx)
- ProviderCard 的名称、辅助文本、当前状态与动作各有稳定位置，适合借鉴到服务名、地址、运行归属和打开/启停操作。[ProviderCard](https://github.com/farion1231/cc-switch/blob/51fce968bca807aeee93be9420c651afb582c2c8/src/components/providers/ProviderCard.tsx)
- 已查看仓内官方 `main-zh.png` 截图：顶部应用 tabs 与较大的独立卡片。该截图和当前已引入 Sidebar 的源码存在布局代差，因此不把截图当作当前 main 完整界面证据。[官方截图](https://github.com/farion1231/cc-switch/blob/51fce968bca807aeee93be9420c651afb582c2c8/assets/screenshots/main-zh.png)

对 LocalServices 的方向：借鉴侧栏和统一页头；项目选择与全局「共享资源 / Agent 连接 / 域名 / 设置」分层；避免所有能力堆到总览卡片。服务多时优先紧凑行，较大卡片可只用于项目摘要。

## 4. Magpie：哪些值得借鉴

核查版本：`yetone/magpie` main commit `2741b749f95c8548d1f9107c5cfe474ed1d05c8f`。

- 当前源码实际是 Go + Wails v3 beta.24、嵌入 HTML/CSS/JS 与系统 WebView，包含菜单栏面板和主窗口；不能按早期实现或「看起来原生」推断为 SwiftUI。[go.mod](https://github.com/yetone/magpie/blob/2741b749f95c8548d1f9107c5cfe474ed1d05c8f/go.mod)、[app.go](https://github.com/yetone/magpie/blob/2741b749f95c8548d1f9107c5cfe474ed1d05c8f/internal/gui/app.go)
- 当前 HTML 的导航按 Work / Models / Insights / Extend 分组，支持侧栏及窄窗口顶部导航；主列表与设置页分开。[index.html](https://github.com/yetone/magpie/blob/2741b749f95c8548d1f9107c5cfe474ed1d05c8f/internal/gui/assets/index.html)
- 实际查看的官方 Agents 主窗口与菜单栏截图具有共同语法：浅灰基底、白色整组列表、细分隔线、系统字体、左侧图标与名称、低对比辅助状态、右侧行内选择/操作。菜单栏减少说明并保留高频动作。[主窗口截图](https://github.com/yetone/magpie/blob/2741b749f95c8548d1f9107c5cfe474ed1d05c8f/site/public/img/agents-light.png)、[菜单栏截图](https://github.com/yetone/magpie/blob/2741b749f95c8548d1f9107c5cfe474ed1d05c8f/site/public/img/panel-light.png)
- CSS 用语义 token 区分背景、容器、分隔线、正文、辅助文本和状态，支持明暗主题；失败状态不仅用红色，还用形状表达。[app.css](https://github.com/yetone/magpie/blob/2741b749f95c8548d1f9107c5cfe474ed1d05c8f/internal/gui/assets/app.css)

对 LocalServices 的方向：服务行表达「Buildbase → buildbase.localhost → 外部运行/受管运行 → 打开 + 启停 + 更多」；日志/依赖/端口/PID/接管细节进详情，不让工程信息挤占第一眼。菜单栏优先收藏与异常服务，主窗口承担登记、分析、资源与恢复流程。开关只表达明确且可逆的单一状态；接管/恢复/移除不得伪装成无副作用开关。

## 5. 成本路线：估算而非项目工时承诺

假设单名熟悉现有代码的开发者，桌面和浏览器两入口一起验收，不包含设计等待、跨平台支持或完全迁移后端。下面是外部研究给主分析的区间，最终排期应结合现有 UI 与引擎耦合复核。

| 路线 | 预计工作 | 粗估 |
|---|---|---|
| 保留壳与现有前端，统一样式 | tokens、页头、密度、状态、服务行和重点页面 | 3–5 人日；复杂交互原样保留时 |
| 保留壳，拆分并系统改造 Web UI | API client / UI state / views 分开，导航、详情、错误与长任务、回归测试 | 8–15 人日 |
| 保留壳，将前端迁为 React/TypeScript | 额外引入构建、打包、桥接适配及现有流程迁移 | 通常 12–20 人日；并非获得参考外观的前提 |
| 更换 Tauri/Wails 壳 | 另加 Python daemon sidecar、更新/签名、菜单栏、桥接、文件选择与权限适配 | 总量约 20–30+ 人日，收益主要在未来跨平台 |

这些不是参考项目公开数据，均为依据本项目现状与迁移范围给出的工程判断。优先前两条；无需为了「像 CC Switch」就引入它的整套栈，也无需为了「像 Magpie」就迁移 Go。

## 6. 许可证与复用

已实际读取两仓 LICENSE，均为 MIT：允许复制、修改和分发代码，但复用代码或实质性部分时需保留原版权及许可声明。[CC Switch LICENSE](https://github.com/farion1231/cc-switch/blob/51fce968bca807aeee93be9420c651afb582c2c8/LICENSE)、[Magpie LICENSE](https://github.com/yetone/magpie/blob/2741b749f95c8548d1f9107c5cfe474ed1d05c8f/LICENSE)

建议优先复用交互原则与布局思路，用 LocalServices 自身名称/图标重新实现；如实际复制组件/CSS，在项目第三方声明中记载来源并保留许可。MIT 文件没有提供一份单独的品牌/商标授权，不应把其 Logo 当成本项目品牌。

## 7. 本次验证边界

已完成：固定版本源码核查、LICENSE 核查、三张官方截图实际查看、标准/框架/代理官方文档核查。引用主分析提供的本机 DNS/端口绑定/浏览器探测结果，明确限于对应机器和入口。

未做：部署代理、修改 hosts/resolver/信任库、改服务清单、迁移 UI 或运行应用测试。因此域名生命周期集成、HMR、登录兼容、Safari 的具体本机行为仍是后续实现验收项。

## 8. 更新：无端口的品牌后缀域名

最新目标形态为 `http://buildbase.locove` 或 `https://buildbase.locove`，地址不出现端口号。用户已确认品牌为 Locove／洛湾；HTTP/HTTPS 的阶段范围见实施 Goal。业务进程仍可监听各自的内部端口，浏览器仅访问统一入口。

### 8.1 系统级域名解析与 HTTP 路由分开

任意品牌后缀不具有 `.localhost` 的自动回环语义，必须由工具安装本地域名解析。macOS 首选可验证的 split DNS：只把 `.locove` 查询交给本地 DNS，其余互联网域名保持系统原有解析策略。安装示例（仅文档，未写入系统）：

```text
# /etc/resolver/locove
domain locove
nameserver 127.0.0.1
port 53535
```

已直接阅读本机 macOS 26.6 的 `man 5 resolver`：文件名可作为域名；显式 `domain` 也可指定域；解析按最长后缀匹配；独立 `port` 指令支持非默认端口，默认才是 53。同一规则也见 Apple 公开源码的 `resolver.5`。因此本地 DNS 可先验证使用高位端口，不必假设必须占用系统 53；本机实际转发到该非默认端口的行为尚未安装验证。[Apple resolver.5 源码](https://github.com/apple-oss-distributions/libresolv/blob/main/resolver.5)

Apple 自己的 container 工具也管理 `/etc/resolver`，其公开维护者讨论展示 `nameserver 127.0.0.1`、`port 2053` 的现有配置。该证据支持这是实际采用的系统接入方式，不替代我们对目标 macOS/浏览器的回归验证。[Apple container DNS 管理源码](https://github.com/apple/container/blob/main/Sources/ContainerCommands/System/DNS/DNSCreate.swift)、[维护者对 resolver 配置的说明](https://github.com/apple/container/issues/1407)

完整链路：

```text
buildbase.locove
  → macOS 按 locove 后缀把查询发给本地 DNS
  → DNS 返回实际已监听的回环 A/AAAA
  → 浏览器按 http/https 默认端口连接统一入口
  → 入口精确匹配 Host buildbase.locove
  → 转发至已登记且归属验证通过的 Buildbase endpoint
```

DNS 应只服务选定后缀，匹配登记表中的有效服务；未登记名字返回明确不存在。代理独立拒绝未知 Host，DNS 不构成代理访问控制。HTTP 入口需监听 80，HTTPS 入口需监听 443；本机前述端口权限问题需要由系统安装模块处理。系统安装、修复、卸载应有明确归属，只删除工具自己的 resolver 配置，不覆盖用户其他文件；不更改 `/etc/resolv.conf` 或全局网络 DNS。后者由 macOS 自动维护，不应手工覆盖。[Apple resolver.5](https://github.com/apple-oss-distributions/libresolv/blob/main/resolver.5)、[HTTP 默认端口规范](https://www.rfc-editor.org/rfc/rfc9110.html#name-http-uri-scheme)

### 8.2 动态 hosts 与 split DNS 的区别

| 维度 | 工具管理 hosts 条目 | 后缀 resolver + 本地 DNS |
|---|---|---|
| 表达能力 | 对每个完整域名维护 IP 映射；不能用一个 `*.locove` 条目实现通配 | 一次配置接管选定后缀，本地 DNS 决定登记名与动态记录 |
| 系统写入 | 新建/重命名/移除域名时更新 hosts 受管块 | 安装/更换后缀时更新 resolver；日常登记可只更新工具自己的路由表 |
| 常驻依赖 | 无额外 DNS 进程；仍需要 HTTP 反代入口 | 本地 DNS 与 HTTP 反代入口都要监督运行 |
| 失败表现 | 易遗留旧地址；需避免覆盖人工条目 | DNS 未运行会解析失败；需有自检与恢复 |
| 初版适用 | 少量固定服务的简化路径，实施较少 | 面向可持续登记和品牌后缀的正式产品路径 |

本机 `man 5 hosts` 明确 hosts 是逐主机 IP、名称、别名的数据表，由 mDNSResponder 用于 `getaddrinfo` 等系统解析。Apple 源码说明 mDNSResponder 是 macOS 系统 resolver。[Apple mDNSResponder 项目说明](https://github.com/apple-oss-distributions/mDNSResponder)

工程判断：正式方案优先 split DNS；hosts 可作为已登记少量名字的兼容实现，但不能代替系统功能验收，也不应伪装成通配 DNS。

### 8.3 品牌后缀、保留后缀和 HTTP/HTTPS

截至本次查询，`LOCOVE` 不在 IANA 已委派 TLD 清单，也不在 IANA 特殊用途域名登记中。因此它可以作为工具本机接管的自定义后缀实现，但不是标准保留的私有 TLD；将来命名冲突/委派变化需要可迁移后缀设计。这个事实不阻断两段品牌域名方案。[IANA 当前 TLD 清单](https://data.iana.org/TLD/tlds-alpha-by-domain.txt)、[IANA Special-Use Domain Names](https://www.iana.org/assignments/special-use-domain-names)

`.test` 及其子域是 RFC/IANA 保留的测试命名空间，可作为用户愿意选择时的替代方案，例如 `buildbase.test`，或三段的 `buildbase.locove.test`；它不是本次用户所要求的品牌后缀主方案。`.test` 仍需本地解析，不会像 `.localhost` 一样自动回环。[RFC 6761 §6.2](https://www.rfc-editor.org/rfc/rfc6761.html#section-6.2)

不要拿 `.dev` 或 `.app` 当作免费的任意本地后缀：它们是公共 TLD，且整个 TLD 在 HSTS preload 中，普通 HTTP 方案会遇到浏览器强制 HTTPS。若确实拥有某个公共域名且配好 HTTPS，当然能用于本地解析，但那是另一种产品部署方式。[Google Registry .dev](https://www.registry.google/domains/dev/)、[Google Registry .app](https://www.registry.google/domains/app/)

不选 `.local` 作为此处的普通私有 DNS 后缀，因为它属于 mDNS 特殊用途，会进入 Bonjour/链路本地发现语义。[RFC 6762 §3](https://www.rfc-editor.org/rfc/rfc6762.html#section-3)

另一个实质区别：任意 `http://buildbase.locove` 即使 DNS 返回回环 IP，也不自动享有 `.localhost` 主机名在 Secure Contexts 算法中的特例；算法明确检查 IP 字面量或 localhost 类主机名，不因任意域名解析到回环就授予同样待遇。普通页面可先使用 HTTP，但需要受安全上下文限制的能力时，应将本地 HTTPS、可信根证书、证书更新与清理纳入正式方案，而不是假定 HTTP 功能完全相同。[W3C Secure Contexts §3.1](https://www.w3.org/TR/secure-contexts/#is-origin-trustworthy)

### 8.4 DoH 和浏览器验收范围

不能承诺本地 resolver 配好后所有浏览器设置都自动生效。Chromium 的官方源码文档说明它具有自带 DNS 客户端、系统 resolver、hosts 读取等不同路径，Secure DNS 模式会影响来源选择；Firefox 官方管理员文档明确提供 DoH 的 `ExcludedDomains`，使内网后缀转交系统 resolver，并区分有无 fallback。[Chromium Host Resolution](https://github.com/chromium/chromium/blob/main/net/dns/README.md)、[Firefox DNSOverHTTPS 官方策略](https://firefox-admin-docs.mozilla.org/reference/policies/dnsoverhttps/)

最低验收矩阵应覆盖：Chrome/Edge 默认 Secure DNS 与指定 DoH 提供商、Firefox 默认及严格 DoH、Safari、App 内 WKWebView，以及用户实际启用的 VPN/HTTP 代理。用系统 `getaddrinfo` 和真实浏览器分别验证；只对本地 DNS 执行 `dig @127.0.0.1 -p 53535` 只能证明 DNS 进程响应，不能证明 split DNS 或浏览器实际用了它。

诊断页应区分「DNS 进程可用」「系统后缀生效」「浏览器能访问」「服务 upstream 健康」，再给与当前浏览器匹配的修复办法；不要通过全局关闭用户 Secure DNS 作为默认安装动作。验收仍须包含 HMR、SSE、登录回调、Cookie/CORS、移除登记、卸载及域名更名。

本次没有写 `/etc/resolver`、hosts、系统信任库、浏览器策略或任何系统网络配置，也没有安装上述 DNS/80/443 代理。

## 9. 已保存的视觉参考

接续时直接使用仓库内的 [参考截图与来源说明](assets/locove-ui-references/README.md)，不依赖 `/tmp/localservices-reference-research`。截图用于理解布局与交互；CC Switch 截图较旧，与当前 Sidebar 源码应分别解读。用户确认品牌名称没有指定必须复制某一截图或迁移到参考项目技术栈。
