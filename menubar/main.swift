// Locove／洛湾（LocalServices）：菜单栏 + 主窗口（单进程，LSUIElement）。
// 菜单栏按 registry 的 Project 分组（收藏/运行中在前，"需要处理"其次），
// 主窗口由 MainWindow.swift 的 WKWebView 承载 daemon 工作台。

import AppKit
import SwiftUI

let port = ProcessInfo.processInfo.environment["LOCALSERVICES_PORT"] ?? "47800"
let base = URL(string: "http://127.0.0.1:\(port)")!
// token 与 daemon 的 paths.home()/token 同源：生产缺省 ~/Library/Application Support/LocalServices，
// 隔离验证可经 LOCALSERVICES_HOME 指向测试目录（scripts/verify_w3_native.sh）。
let tokenDir = ProcessInfo.processInfo.environment["LOCALSERVICES_HOME"]
    ?? (NSHomeDirectory() as NSString).appendingPathComponent("Library/Application Support/LocalServices")
let tokenPath = (tokenDir as NSString).appendingPathComponent("token")

enum InterfaceLanguage: String {
    case chinese = "zh-CN"
    case english = "en"

    var productName: String { text("洛湾", "Locove") }

    func text(_ chinese: String, _ english: String) -> String {
        self == .english ? english : chinese
    }

    func serviceLabel(_ service: Service) -> String {
        guard self == .english else { return service.label }
        if service.label == "引擎无响应" { return "Engine unavailable" }
        return ["available": "Available", "partial": "Degraded", "failed": "Failed",
                "stopped": "Stopped", "conflict": "Conflict", "external": "Running externally",
                "starting": "Starting", "stopping": "Stopping", "preparing": "Preparing",
                "unknown": "Unknown"][service.state] ?? service.label
    }
}

struct Service {
    let id: String, name: String, state: String, label: String, intent: String
    let codeState: String, codeLabel: String, reason: String
    let observeOnly: Bool, canOpen: Bool, canTakeover: Bool, op: String?

    var running: Bool { ["available", "partial", "external"].contains(state) }
    var needsAttention: Bool { ["failed", "conflict"].contains(state) }
    var busy: Bool { op != nil || ["starting", "stopping", "preparing"].contains(state) }
}

struct WorkspaceProject {
    let id: String, name: String, favorite: Bool, agent: String, serviceIds: [String]
}

final class App: NSObject, NSApplicationDelegate, NSWindowDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let statusLogo = NSImage(named: "LocoveStatus")
    private let panelModel = StatusPanelModel()
    private var panelEvents: [Any] = []
    private lazy var statusPanel: StatusPanelWindow = {
        let panel = StatusPanelWindow(contentRect: NSRect(origin: .zero, size: StatusPanel.size),
                                      styleMask: [.borderless, .nonactivatingPanel],
                                      backing: .buffered, defer: true)
        panel.delegate = self
        panel.level = .popUpMenu
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
        panel.contentViewController = NSHostingController(rootView: StatusPanel(model: panelModel, app: self))
        return panel
    }()
    private lazy var quickServicesPanel = QuickServicesPanel()
    let mainWindow = MainWindowController(base: base, portString: port)
    var services: [Service] = []
    var projects: [WorkspaceProject] = []
    var connected = false
    var attention = false
    var authenticationFailed = false
    private var refreshVersion = 0
    var warnings: [String] = []
    let session: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.connectionProxyDictionary = [:]
        c.timeoutIntervalForRequest = 5
        return URLSession(configuration: c)
    }()

    func applicationDidFinishLaunching(_ n: Notification) {
        if let button = item.button {
            button.target = self
            button.action = #selector(statusItemClicked(_:))
            button.sendAction(on: [.leftMouseDown, .rightMouseUp])
        }
        mainWindow.onLanguageChanged = { [weak self] in self?.refresh() }
        installMainMenu() // ⌘N/⌘R/⌘Q 等在主窗口聚焦时生效
        for shortcut in mainWindow.shortcuts {
            shortcut.conflictingAction = { [weak self, weak shortcut] key in
                self?.mainWindow.shortcuts.first { $0 !== shortcut && $0.isActive && $0.saved == key }?.action
            }
            shortcut.onOpen = { [weak self, weak shortcut] in
                guard let self, let shortcut, let key = shortcut.saved,
                      !mainWindow.recordShortcutIfEditing(key) else { return }
                switch shortcut.action {
                case .mainWindow: showMainWindow()
                case .quickServices: showQuickServices()
                }
            }
            shortcut.restore()
        }
        updateIcon()
        refresh()
        mainWindow.show()
        Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in self?.refresh() }
    }

    @objc private func statusItemClicked(_ button: NSStatusBarButton) {
        if statusPanel.isVisible {
            closeStatusPanel()
            return
        }
        guard let window = button.window, let screen = window.screen ?? NSScreen.main else { return }
        let anchor = window.convertToScreen(button.convert(button.bounds, to: nil))
        let area = screen.visibleFrame
        let size = StatusPanel.size
        let left = min(max(anchor.midX - size.width / 2, area.minX + 6), area.maxX - size.width - 6)
        let top = min(area.maxY, anchor.minY) - 4
        let frame = NSRect(origin: NSPoint(x: left, y: max(area.minY + 4, top - size.height)), size: size)
        statusPanel.setFrame(frame, display: true)
        statusPanel.makeKeyAndOrderFront(nil)
        if let esc = NSEvent.addLocalMonitorForEvents(matching: .keyDown, handler: { [weak self] event in
            guard event.keyCode == 53, event.window === self?.statusPanel else { return event }
            self?.closeStatusPanel()
            return nil
        }) {
            panelEvents.append(esc)
        }
        if let outside = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown], handler: { [weak self] _ in
            self?.closeStatusPanel()
        }) {
            panelEvents.append(outside)
        }
        refresh()
    }

    func closeStatusPanel() {
        guard statusPanel.isVisible else { return }
        panelEvents.forEach(NSEvent.removeMonitor)
        panelEvents.removeAll()
        statusPanel.orderOut(nil)
    }

    func windowDidResignKey(_ notification: Notification) { closeStatusPanel() }

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
        refreshVersion += 1
        let version = refreshVersion
        request("GET", "api/status") { [weak self] code, obj in
            guard let self, self.refreshVersion == version else { return }
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
                                   reason: d["reason"] as? String ?? "",
                                   observeOnly: d["observe_only"] as? Bool ?? false,
                                   canOpen: d["can_open"] as? Bool ?? false,
                                   canTakeover: d["can_takeover"] as? Bool ?? false, op: d["op"] as? String)
                }
            } else {
                self.connected = false
                self.services = []
                self.authenticationFailed = code == 401
            }
            self.quickServicesPanel.update(self.openableRunningServices)
            self.request("GET", "api/registry") { [weak self] code2, obj2 in
                guard let self, self.refreshVersion == version else { return }
                if code2 == 200, let reg = obj2?["registry"] as? [String: Any],
                   let list = reg["projects"] as? [[String: Any]] {
                    let settings = reg["settings"] as? [String: Any]
                    let language = InterfaceLanguage(rawValue: settings?["language"] as? String ?? "") ?? .english
                    if self.panelModel.language != language {
                        self.panelModel.language = language
                        self.mainWindow.setLanguage(language)
                        self.installMainMenu()
                    }
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
                self.panelModel.connected = self.connected
                self.panelModel.connection = self.connected ? self.text("守护进程运行中", "Daemon running")
                    : self.authenticationFailed ? self.text("认证失败", "Authentication failed")
                    : self.text("无法连接控制服务", "Daemon unavailable")
                self.panelModel.warnings = self.connected ? self.warnings : []
                self.panelModel.sections = self.workbenchSections()
            }
        }
    }

    func updateIcon() {
        let name = panelModel.language.productName
        let desc: String
        if !connected {
            desc = name + text("：无法连接控制服务", " — Daemon unavailable")
        } else if attention {
            desc = name + text("：有项目需要处理", " — Projects need attention")
        } else {
            desc = name
        }

        let badgeSymbol = !connected ? "questionmark.circle.fill" : attention ? "exclamationmark.circle.fill" : nil
        let badge = badgeSymbol.flatMap { NSImage(systemSymbolName: $0, accessibilityDescription: nil) }
        let image: NSImage?
        if let statusLogo {
            image = NSImage(size: NSSize(width: 23, height: 19), flipped: false) { _ in
                // 原图有透明留白，断连徽标放在湾口上方。
                statusLogo.draw(in: NSRect(x: 0, y: -2, width: 24, height: 24))
                badge?.draw(in: NSRect(x: 14, y: 9, width: 9, height: 9))
                return true
            }
        } else {
            image = badge ?? NSImage(systemSymbolName: "square.stack.3d.up", accessibilityDescription: nil)
        }
        image?.isTemplate = true
        item.button?.image = image
        item.button?.setAccessibilityLabel(desc)
        item.button?.toolTip = desc
    }

    private func text(_ chinese: String, _ english: String) -> String {
        panelModel.language.text(chinese, english)
    }

    private func workbenchSections() -> [StatusPanelSection] {
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
            entries.append((WorkspaceProject(id: "", name: text("未分组服务", "Ungrouped services"), favorite: false, agent: "none",
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
        var out: [StatusPanelSection] = []
        let front = sort(entries.filter { rank($0) == 0 })
        let needs = sort(entries.filter { rank($0) == 1 })
        let rest = sort(entries.filter { rank($0) == 2 })
        if !front.isEmpty { out.append(StatusPanelSection(title: text("收藏与运行中", "Favorites and running"), projects: front)) }
        if !needs.isEmpty { out.append(StatusPanelSection(title: text("需要处理", "Needs attention"), projects: needs)) }
        if !rest.isEmpty { out.append(StatusPanelSection(title: text("其他项目", "Other projects"), projects: rest)) }
        return out
    }

    func projectSummary(_ members: [Service]) -> String {
        if members.contains(where: \.busy) { return text("操作中", "Working") }
        if members.contains(where: \.needsAttention) { return text("需要处理", "Needs attention") }
        if members.allSatisfy(\.running) { return text("运行中", "Running") }
        if members.allSatisfy({ $0.state == "stopped" }) { return text("已停止", "Stopped") }
        let run = members.filter(\.running).count
        return run > 0 ? text("\(run) 运行中", "\(run) running") : text("已停止", "Stopped")
    }

    func primaryTitle(_ service: Service) -> String {
        if service.busy { return text("操作中", "Working") }
        switch service.state {
        case "available", "partial", "external":
            return service.canOpen ? text("打开", "Open") : text("查看详情", "Details")
        case "stopped" where !service.observeOnly:
            return text("启动并打开", "Start and open")
        case "failed", "conflict":
            return text("查看原因", "View issue")
        default:
            return text("查看详情", "Details")
        }
    }

    func performOperation(_ sid: String, _ action: String) {
        closeStatusPanel()
        if action == "takeover" {
            let alert = NSAlert()
            alert.messageText = text("接管这个外部启动的实例？", "Take over this externally started instance?")
            alert.informativeText = text("会停止该实例并由本工具从最新源码启动；有活动任务时等空闲再接管。", "Locove will stop this instance and start it from the current source once it is idle.")
            alert.addButton(withTitle: text("接管", "Take over"))
            alert.addButton(withTitle: text("取消", "Cancel"))
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
            guard let self else { return }
            if code != 200 {
                let alert = NSAlert()
                alert.messageText = self.text("操作未执行", "Action failed")
                alert.informativeText = response?["error"] as? String ?? self.text("无法连接控制服务", "Daemon unavailable")
                alert.runModal()
            }
            self.refresh()
        }
    }

    func activateService(_ id: String) {
        guard let p = services.first(where: { $0.id == id }), !p.busy, p.state != "unknown" else { return }
        closeStatusPanel()
        switch p.state {
        case "available" where p.canOpen, "partial" where p.canOpen, "external" where p.canOpen:
            performOperation(id, "open")
        case "stopped" where !p.observeOnly:
            performOperation(id, "start")
        default:
            // W2/W3：失败/冲突「查看原因」与浏览器同一诊断路径（展开 + 聚焦诊断行）
            showMainWindow()
            mainWindow.evaluate("window.lsDiagnose && window.lsDiagnose('\(id)')")
        }
    }

    @objc func newProject() {
        showMainWindow()
        mainWindow.evaluate("window.lsOpenView && window.lsOpenView('new-project')")
    }

    @objc func showMainWindow() {
        quickServicesPanel.dismiss()
        closeStatusPanel()
        mainWindow.show()
    }

    private var openableRunningServices: [Service] {
        services.filter { $0.running && $0.canOpen && !$0.busy }.sorted {
            $0.name.localizedStandardCompare($1.name) == .orderedAscending
        }
    }

    @objc func showQuickServices() {
        closeStatusPanel()
        quickServicesPanel.show(services: openableRunningServices, language: panelModel.language) { [weak self] id in
            guard let self, openableRunningServices.contains(where: { $0.id == id }) else { return }
            performOperation(id, "open")
        }
        if quickServicesPanel.isVisible { refresh() }
    }

    @objc func showSettings() {
        showMainWindow()
        mainWindow.evaluate("window.lsOpenView && window.lsOpenView('settings')")
    }

    @objc func openDashboard() {
        closeStatusPanel()
        NSWorkspace.shared.open(URL(string: base.absoluteString + "/")!)
    }

    @objc func refreshNow() {
        request("POST", "api/refresh") { [weak self] _, _ in self?.refresh() }
    }

    @objc func quit() { NSApp.terminate(nil) }

    /// 应用主菜单：主窗口聚焦时 ⌘N（打开主窗口）等快捷键生效；编辑菜单供 WebView 文本输入使用。
    func installMainMenu() {
        let mainMenu = NSMenu()
        let appItem = NSMenuItem(title: panelModel.language.productName, action: nil, keyEquivalent: "")
        let appSub = NSMenu()
        for (t, s, k) in [(text("打开主窗口", "Open main window"), #selector(showMainWindow), "n"),
                          (ShortcutAction.quickServices.title(in: panelModel.language), #selector(showQuickServices), ""),
                          (text("设置…", "Settings…"), #selector(showSettings), ","),
                          (text("添加项目…", "Add project…"), #selector(newProject), ""),
                          (text("在浏览器中打开", "Open in browser"), #selector(openDashboard), "d"),
                          (text("刷新状态", "Refresh status"), #selector(refreshNow), "r")] {
            let m = NSMenuItem(title: t, action: s, keyEquivalent: k)
            m.target = self
            appSub.addItem(m)
        }
        appSub.addItem(.separator())
        let quit = NSMenuItem(title: text("退出菜单栏（后台服务继续运行）", "Quit menu bar app (services keep running)"), action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        appSub.addItem(quit)
        appItem.submenu = appSub
        mainMenu.addItem(appItem)

        let editItem = NSMenuItem(title: text("编辑", "Edit"), action: nil, keyEquivalent: "")
        let editSub = NSMenu()
        for (t, a, k) in [(text("撤销", "Undo"), Selector(("undo:")), "z"), (text("重做", "Redo"), Selector(("redo:")), "Z"),
                          (text("剪切", "Cut"), #selector(NSText.cut(_:)), "x"), (text("拷贝", "Copy"), #selector(NSText.copy(_:)), "c"),
                          (text("粘贴", "Paste"), #selector(NSText.paste(_:)), "v"), (text("全选", "Select all"), #selector(NSText.selectAll(_:)), "a")] {
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
