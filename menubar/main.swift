// Locove／洛湾（LocalServices）：菜单栏 + 主窗口（单进程，LSUIElement）。
// 菜单栏按 registry 的 Project 分组（收藏/运行中在前，"需要处理"其次），
// 主窗口由 MainWindow.swift 的 WKWebView 承载 daemon 工作台。

import AppKit

let port = ProcessInfo.processInfo.environment["LOCALSERVICES_PORT"] ?? "47800"
let base = URL(string: "http://127.0.0.1:\(port)")!
let tokenPath = (NSHomeDirectory() as NSString).appendingPathComponent("Library/Application Support/LocalServices/token")

struct Service {
    let id: String, name: String, state: String, label: String, intent: String
    let codeState: String, codeLabel: String
    let observeOnly: Bool, canOpen: Bool, canTakeover: Bool, op: String?
    let group: String?, groupName: String?, groupOrder: Int
    let components: [[String: Any]]

    var running: Bool { ["available", "partial", "external"].contains(state) }
    var needsAttention: Bool { ["failed", "conflict"].contains(state) }
    var busy: Bool { op != nil || ["starting", "stopping", "preparing"].contains(state) }
}

struct WorkspaceProject {
    let id: String, name: String, favorite: Bool, agent: String, serviceIds: [String]
}

final class App: NSObject, NSApplicationDelegate, NSMenuDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let menu = NSMenu()
    private let statusLogo = NSImage(named: "LocoveStatus")
    private lazy var quickMenu: NSMenu = {
        let menu = NSMenu()
        let open = menu.addItem(withTitle: "打开主窗口", action: #selector(showMainWindow), keyEquivalent: "")
        open.target = self
        let quit = menu.addItem(withTitle: "退出菜单栏（后台服务继续运行）", action: #selector(quit), keyEquivalent: "")
        quit.target = self
        return menu
    }()
    let mainWindow = MainWindowController(base: base, portString: port)
    var services: [Service] = []
    var projects: [WorkspaceProject] = []
    var connected = false
    var attention = false
    var lastError = ""
    var warnings: [String] = []
    let session: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.connectionProxyDictionary = [:]
        c.timeoutIntervalForRequest = 5
        return URLSession(configuration: c)
    }()

    func applicationDidFinishLaunching(_ n: Notification) {
        menu.delegate = self
        if let button = item.button {
            button.target = self
            button.action = #selector(statusItemClicked(_:))
            button.sendAction(on: [.leftMouseDown, .rightMouseUp])
        }
        installMainMenu() // ⌘N/⌘R/⌘Q 等在主窗口聚焦时生效
        updateIcon()
        rebuild()
        refresh()
        mainWindow.show()
        Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in self?.refresh() }
    }

    @objc private func statusItemClicked(_ button: NSStatusBarButton) {
        let event = NSApp.currentEvent
        let secondaryClick = event?.type == .rightMouseUp
            || (event?.type == .leftMouseDown && event?.modifierFlags.contains(.control) == true)
        // 菜单打开期间交给 AppKit 跟踪，关闭后恢复按钮的点击处理。
        item.menu = secondaryClick ? quickMenu : menu
        defer { item.menu = nil }
        button.performClick(nil)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func token() -> String {
        (try? String(contentsOfFile: tokenPath, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }

    func request(_ method: String, _ path: String, body: [String: Any]? = nil, done: @escaping (Int, [String: Any]?) -> Void) {
        var r = URLRequest(url: base.appendingPathComponent(path))
        r.httpMethod = method
        r.setValue(token(), forHTTPHeaderField: "X-LocalServices-Token")
        r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let body { r.httpBody = try? JSONSerialization.data(withJSONObject: body) }
        session.dataTask(with: r) { data, resp, _ in
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            let obj = data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? nil
            DispatchQueue.main.async { done(code, obj) }
        }.resume()
    }

    func refresh() {
        request("GET", "api/status") { [weak self] code, obj in
            guard let self else { return }
            if code == 200, let obj, let list = obj["projects"] as? [[String: Any]] {
                self.connected = true
                self.attention = obj["attention"] as? Bool ?? false
                self.warnings = obj["system_warnings"] as? [String] ?? []
                self.services = list.map { d in
                    let code = d["code"] as? [String: Any] ?? [:]
                    return Service(id: d["id"] as? String ?? "", name: d["name"] as? String ?? "",
                                   state: d["state"] as? String ?? "unknown", label: d["label"] as? String ?? "未知",
                                   intent: d["intent"] as? String ?? "stopped",
                                   codeState: code["state"] as? String ?? "", codeLabel: code["label"] as? String ?? "",
                                   observeOnly: d["observe_only"] as? Bool ?? false,
                                   canOpen: d["can_open"] as? Bool ?? false,
                                   canTakeover: d["can_takeover"] as? Bool ?? false, op: d["op"] as? String,
                                   group: d["group"] as? String, groupName: d["group_name"] as? String,
                                   groupOrder: d["group_order"] as? Int ?? 0,
                                   components: d["components"] as? [[String: Any]] ?? [])
                }
            } else {
                self.connected = false
                self.services = []
                self.lastError = code == 401 ? "认证失败" : "无法连接控制服务"
            }
            self.request("GET", "api/registry") { [weak self] code2, obj2 in
                guard let self else { return }
                if code2 == 200, let reg = obj2?["registry"] as? [String: Any],
                   let list = reg["projects"] as? [[String: Any]] {
                    self.projects = list.map { d in
                        WorkspaceProject(id: d["id"] as? String ?? "",
                                         name: d["name"] as? String ?? (d["id"] as? String ?? "?"),
                                         favorite: d["favorite"] as? Bool ?? false,
                                         agent: d["agent"] as? String ?? "none",
                                         serviceIds: d["services"] as? [String] ?? [])
                    }
                } else {
                    self.projects = []
                }
                self.updateIcon()
                self.rebuild()
            }
        }
    }

    func updateIcon() {
        let desc: String
        if !connected {
            desc = "Locove／洛湾：无法连接控制服务"
        } else if attention {
            desc = "Locove／洛湾：有项目需要处理"
        } else {
            desc = "Locove／洛湾"
        }

        let disconnectBadge = connected ? nil : NSImage(systemSymbolName: "questionmark.circle",
                                                         accessibilityDescription: nil)
        let image: NSImage?
        if let statusLogo {
            image = NSImage(size: NSSize(width: 23, height: 19), flipped: false) { _ in
                // 原图有透明留白，断连徽标放在湾口上方。
                statusLogo.draw(in: NSRect(x: 0, y: -2, width: 24, height: 24))
                disconnectBadge?.draw(in: NSRect(x: 14, y: 9, width: 9, height: 9))
                return true
            }
        } else {
            image = disconnectBadge ?? NSImage(systemSymbolName: "square.stack.3d.up", accessibilityDescription: nil)
        }
        image?.isTemplate = true
        item.button?.image = image
        item.button?.setAccessibilityLabel(desc)
        item.button?.toolTip = desc
    }

    func menuWillOpen(_ menu: NSMenu) { refresh() }

    // MARK: 菜单栏结构（工作台：Project 分组）

    /// (分区标题, [(Project, 服务列表)])；收藏/运行中在前，"需要处理"其次，其余在后。
    func workbenchSections() -> [(String?, [(WorkspaceProject, [Service])])] {
        var byId: [String: Service] = [:]
        for s in services { byId[s.id] = s }
        var entries: [(WorkspaceProject, [Service])] = []
        var claimed = Set<String>()
        for p in projects.sorted(by: { $0.name < $1.name }) {
            let members = p.serviceIds.compactMap { byId[$0] }
            guard !members.isEmpty else { continue }
            claimed.formUnion(members.map(\.id))
            entries.append((p, members))
        }
        let orphans = services.filter { !claimed.contains($0.id) }
        if !orphans.isEmpty {
            entries.append((WorkspaceProject(id: "", name: "未分组服务", favorite: false, agent: "none",
                                             serviceIds: orphans.map(\.id)), orphans))
        }
        func rank(_ e: (WorkspaceProject, [Service])) -> Int {
            if e.1.contains(where: \.needsAttention) { return 1 }
            if e.0.favorite || e.1.contains(where: { $0.running || $0.busy }) { return 0 }
            return 2
        }
        func sort(_ list: [(WorkspaceProject, [Service])]) -> [(WorkspaceProject, [Service])] {
            list.sorted { a, b in
                if (a.0.favorite ? 1 : 0) != (b.0.favorite ? 1 : 0) { return a.0.favorite }
                return a.0.name < b.0.name
            }
        }
        var out: [(String?, [(WorkspaceProject, [Service])])] = []
        let front = sort(entries.filter { rank($0) == 0 })
        let needs = sort(entries.filter { rank($0) == 1 })
        let rest = sort(entries.filter { rank($0) == 2 })
        if !front.isEmpty { out.append((nil, front)) }
        if !needs.isEmpty { out.append(("需要处理", needs)) }
        if !rest.isEmpty { out.append(("其他项目", rest)) }
        return out
    }

    func rebuild() {
        menu.removeAllItems()
        let head = NSMenuItem(title: "Locove／洛湾", action: nil, keyEquivalent: "")
        head.isEnabled = false
        menu.addItem(head)
        add("刷新状态", #selector(refreshNow), "r", symbol: "arrow.clockwise")
        add("打开主窗口", #selector(showMainWindow), "n", symbol: "macwindow")
        add("设置…", #selector(showSettings), ",", symbol: "gearshape")
        menu.addItem(.separator())
        if !connected {
            let m = NSMenuItem(title: "⚠︎ \(lastError.isEmpty ? "无法连接控制服务" : lastError)（项目状态未知）", action: nil, keyEquivalent: "")
            m.isEnabled = false
            menu.addItem(m)
        } else {
            for w in warnings {
                let m = NSMenuItem(title: "⚠︎ " + (w.components(separatedBy: "：").first ?? w), action: #selector(openDashboard), keyEquivalent: "")
                m.target = self
                m.toolTip = w
                menu.addItem(m)
            }
            if !warnings.isEmpty { menu.addItem(.separator()) }
            var firstSection = true
            for (header, entries) in workbenchSections() {
                if !firstSection { menu.addItem(.separator()) }
                firstSection = false
                if let header {
                    let m = NSMenuItem(title: header, action: nil, keyEquivalent: "")
                    m.isEnabled = false
                    menu.addItem(m)
                }
                for (proj, members) in entries { menu.addItem(projectItem(proj, members)) }
            }
        }
        menu.addItem(.separator())
        add("添加项目…", #selector(newProject), "")
        add("在浏览器中打开", #selector(openDashboard), "d")
        add("退出菜单栏（后台服务继续运行）", #selector(quit), "q")
    }

    func projectSummary(_ members: [Service]) -> String {
        if members.contains(where: \.busy) { return "操作中" }
        if members.contains(where: \.needsAttention) { return "需要处理" }
        if members.allSatisfy(\.running) { return "运行中" }
        if members.allSatisfy({ $0.state == "stopped" }) { return "已停止" }
        let run = members.filter(\.running).count
        return run > 0 ? "\(run) 运行中" : "已停止"
    }

    /// Project 菜单项：子菜单 = 服务列表（沿用 projectItem 逻辑的主操作 + ≤3 项次级操作）。
    func projectItem(_ p: WorkspaceProject, _ members: [Service]) -> NSMenuItem {
        let title = (p.favorite ? "★ " : "") + p.name + " — " + projectSummary(members)
        let m = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        let sub = NSMenu()
        for s in members.sorted(by: { a, b in
            if a.needsAttention != b.needsAttention { return a.needsAttention }
            return a.name < b.name
        }) {
            sub.addItem(serviceItem(s))
        }
        m.submenu = sub
        return m
    }

    func serviceItem(_ s: Service) -> NSMenuItem {
        var title = "\(s.name) — \(s.label)"
        if s.codeState == "pending" { title += " · 待更新" }
        if s.codeState == "failed" { title += " · 更新失败" }
        let m = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        if s.needsAttention {
            m.image = NSImage(systemSymbolName: "exclamationmark.triangle", accessibilityDescription: "故障")
        }
        let sub = NSMenu()
        sub.addItem(mainActionItem(s))
        // 次级操作上限三项：接管（外部时）/ 停止 / 重启 / 日志
        var secondary: [NSMenuItem] = []
        if s.canTakeover { secondary.append(operationItem("接管外部实例…", ["service": s.id, "action": "takeover"])) }
        if s.intent == "running" && !s.observeOnly && s.state != "external" {
            secondary.append(operationItem("停止", ["service": s.id, "action": "stop"]))
        }
        if ["available", "partial"].contains(s.state) && !s.observeOnly {
            secondary.append(operationItem("重启", ["service": s.id, "action": "restart"]))
        }
        secondary.append(operationItem("日志（工作台）", ["service": s.id, "action": "logs"]))
        if secondary.count > 1 { sub.addItem(.separator()) }
        for item in secondary.prefix(3) {
            item.isEnabled = item.title == "日志（工作台）" || !s.busy
            sub.addItem(item)
        }
        m.submenu = sub
        m.isEnabled = !s.busy
        return m
    }

    /// 主操作（projectItem 逻辑）：停止→启动并打开、可用→打开、外部→打开、失败→查看原因跳工作台。
    func mainActionItem(_ s: Service) -> NSMenuItem {
        let m: NSMenuItem
        switch s.state {
        case "available" where s.canOpen, "partial" where s.canOpen, "external" where s.canOpen:
            m = NSMenuItem(title: "打开", action: #selector(clickProject(_:)), keyEquivalent: "")
        case "stopped" where !s.observeOnly:
            m = NSMenuItem(title: "启动并打开", action: #selector(clickProject(_:)), keyEquivalent: "")
        case "failed", "conflict":
            m = NSMenuItem(title: "查看原因（跳转工作台）", action: #selector(clickProject(_:)), keyEquivalent: "")
        default:
            m = NSMenuItem(title: "打开工作台查看", action: #selector(clickProject(_:)), keyEquivalent: "")
        }
        m.target = self
        m.representedObject = s.id
        m.isEnabled = !s.busy && s.state != "unknown"
        return m
    }

    func operationItem(_ title: String, _ operation: [String: String]) -> NSMenuItem {
        let m = NSMenuItem(title: title, action: #selector(operate(_:)), keyEquivalent: "")
        m.target = self
        m.representedObject = operation
        return m
    }

    @objc func operate(_ sender: NSMenuItem) {
        guard let op = sender.representedObject as? [String: String],
              let sid = op["service"], let action = op["action"] else { return }
        if action == "takeover" {
            let alert = NSAlert()
            alert.messageText = "接管这个外部启动的实例？"
            alert.informativeText = "会停止该实例并由本工具从最新源码启动；有活动任务时等空闲再接管。"
            alert.addButton(withTitle: "接管")
            alert.addButton(withTitle: "取消")
            if alert.runModal() != .alertFirstButtonReturn { return }
        }
        if action == "logs" {
            showMainWindow()
            mainWindow.evaluate("window.lsOpenView && window.lsOpenView('all'); window.lsShowLogs && window.lsShowLogs('\(sid)')")
            return
        }
        var body: [String: Any] = [:]
        if action == "start" { body["open"] = true }
        request("POST", "api/projects/\(sid)/\(action)", body: body) { [weak self] code, response in
            if code != 200 {
                let alert = NSAlert()
                alert.messageText = "操作未执行"
                alert.informativeText = response?["error"] as? String ?? "无法连接控制服务"
                alert.runModal()
            }
            self?.refresh()
        }
    }

    func add(_ t: String, _ s: Selector, _ k: String, symbol: String? = nil) {
        let m = NSMenuItem(title: t, action: s, keyEquivalent: k)
        m.target = self
        if let symbol {
            m.image = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)
        }
        menu.addItem(m)
    }

    @objc func clickProject(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String, let p = services.first(where: { $0.id == id }) else { return }
        switch p.state {
        case "available" where p.canOpen, "partial" where p.canOpen, "external" where p.canOpen:
            request("POST", "api/projects/\(id)/open") { _, _ in }
        case "stopped" where !p.observeOnly:
            request("POST", "api/projects/\(id)/start", body: ["open": true]) { [weak self] _, _ in self?.refresh() }
        default:
            showMainWindow()
            mainWindow.evaluate("window.lsFocusService && window.lsFocusService('\(id)')")
        }
    }

    @objc func newProject() {
        showMainWindow()
        mainWindow.evaluate("window.lsOpenView && window.lsOpenView('new-project')")
    }

    @objc func showMainWindow() { mainWindow.show() }

    @objc func showSettings() {
        showMainWindow()
        mainWindow.evaluate("window.lsOpenView && window.lsOpenView('settings')")
    }

    @objc func openDashboard() {
        NSWorkspace.shared.open(URL(string: base.absoluteString + "/")!)
    }

    @objc func refreshNow() {
        request("POST", "api/refresh") { [weak self] _, _ in self?.refresh() }
    }

    @objc func quit() { NSApp.terminate(nil) }

    /// 应用主菜单：主窗口聚焦时 ⌘N（打开主窗口）等快捷键生效；编辑菜单供 WebView 文本输入使用。
    func installMainMenu() {
        let mainMenu = NSMenu()
        let appItem = NSMenuItem(title: "MyBrain Services", action: nil, keyEquivalent: "")
        let appSub = NSMenu()
        for (t, s, k) in [("打开主窗口", #selector(showMainWindow), "n"),
                          ("设置…", #selector(showSettings), ","),
                          ("添加项目…", #selector(newProject), ""),
                          ("在浏览器中打开", #selector(openDashboard), "d"),
                          ("刷新状态", #selector(refreshNow), "r")] {
            let m = NSMenuItem(title: t, action: s, keyEquivalent: k)
            m.target = self
            appSub.addItem(m)
        }
        appSub.addItem(.separator())
        let quit = NSMenuItem(title: "退出菜单栏（后台服务继续运行）", action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        appSub.addItem(quit)
        appItem.submenu = appSub
        mainMenu.addItem(appItem)

        let editItem = NSMenuItem(title: "编辑", action: nil, keyEquivalent: "")
        let editSub = NSMenu()
        for (t, a, k) in [("撤销", Selector(("undo:")), "z"), ("重做", Selector(("redo:")), "Z"),
                          ("剪切", #selector(NSText.cut(_:)), "x"), ("拷贝", #selector(NSText.copy(_:)), "c"),
                          ("粘贴", #selector(NSText.paste(_:)), "v"), ("全选", #selector(NSText.selectAll(_:)), "a")] {
            editSub.addItem(withTitle: t, action: a, keyEquivalent: k) // target nil → 响应链
        }
        editItem.submenu = editSub
        mainMenu.addItem(editItem)
        NSApp.mainMenu = mainMenu
    }
}

if let bid = Bundle.main.bundleIdentifier,
   NSRunningApplication.runningApplications(withBundleIdentifier: bid)
       .contains(where: { $0.processIdentifier != ProcessInfo.processInfo.processIdentifier }) {
    exit(0)
}

let app = NSApplication.shared
let delegate = App()
app.delegate = delegate
app.setActivationPolicy(.accessory)
app.run()
