// MyBrain Services menu bar: a thin client of the LocalServices daemon (same /api/status as the dashboard).
import AppKit

let port = ProcessInfo.processInfo.environment["LOCALSERVICES_PORT"] ?? "47800"
let base = URL(string: "http://127.0.0.1:\(port)")!
let tokenPath = (NSHomeDirectory() as NSString).appendingPathComponent("Library/Application Support/LocalServices/token")

struct Project {
    let id: String, name: String, state: String, label: String, codeState: String, codeLabel: String
    let observeOnly: Bool, canOpen: Bool, op: String?
    let group: String?, groupName: String?, groupOrder: Int
    let components: [[String: Any]]
}

final class App: NSObject, NSApplicationDelegate, NSMenuDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let menu = NSMenu()
    var projects: [Project] = []
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
        item.menu = menu
        updateIcon()
        rebuild()
        refresh()
        Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in self?.refresh() }
    }

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
                self.projects = list.map { d in
                    let code = d["code"] as? [String: Any] ?? [:]
                    return Project(id: d["id"] as? String ?? "", name: d["name"] as? String ?? "",
                                   state: d["state"] as? String ?? "unknown", label: d["label"] as? String ?? "未知",
                                   codeState: code["state"] as? String ?? "", codeLabel: code["label"] as? String ?? "",
                                   observeOnly: d["observe_only"] as? Bool ?? false,
                                   canOpen: d["can_open"] as? Bool ?? false, op: d["op"] as? String,
                                   group: d["group"] as? String, groupName: d["group_name"] as? String,
                                   groupOrder: d["group_order"] as? Int ?? 0, components: d["components"] as? [[String: Any]] ?? [])
                }
            } else {
                self.connected = false
                self.lastError = code == 401 ? "认证失败" : "无法连接控制服务"
            }
            self.updateIcon()
            self.rebuild()
        }
    }

    func updateIcon() {
        let name: String, desc: String
        if !connected { name = "questionmark.circle"; desc = "MyBrain Services：无法连接控制服务" }
        else if attention { name = "exclamationmark.triangle"; desc = "MyBrain Services：有项目需要处理" }
        else { name = "square.stack.3d.up"; desc = "MyBrain Services" }
        let img = NSImage(systemSymbolName: name, accessibilityDescription: desc)
        img?.isTemplate = true
        item.button?.image = img
        item.button?.toolTip = desc
    }

    func menuWillOpen(_ menu: NSMenu) { refresh() }

    func rebuild() {
        menu.removeAllItems()
        let head = NSMenuItem(title: "MyBrain Services", action: nil, keyEquivalent: "")
        head.isEnabled = false
        menu.addItem(head)
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
            var rendered = Set<String>()
            for p in projects {
                guard let group = p.group else {
                    menu.addItem(projectItem(p))
                    continue
                }
                if rendered.contains(group) { continue }
                rendered.insert(group)
                let members = projects.filter { $0.group == group }.sorted { $0.groupOrder > $1.groupOrder }
                let busy = members.contains { $0.op != nil }
                let groupState = busy ? "操作中" : members.allSatisfy({ $0.state == "available" }) ? "可用" : members.allSatisfy({ $0.state == "stopped" }) ? "已停止" : "需检查"
                let groupItem = NSMenuItem(title: (p.groupName ?? group) + " — " + groupState, action: nil, keyEquivalent: "")
                let sub = NSMenu()
                for member in members { sub.addItem(projectItem(member)) }
                sub.addItem(.separator())
                for (title, action) in [("启动全部", "start"), ("重启全部（图鉴＋Explore 前端＋API）", "restart"), ("停止全部", "stop")] {
                    let item = operationItem(title, ["group": group, "action": action])
                    item.isEnabled = !busy && !members.contains { ["unknown", "external", "conflict"].contains($0.state) }
                    sub.addItem(item)
                }
                sub.addItem(.separator())
                for member in members {
                    let item = operationItem("重启 \(member.name)（全部组件）", ["project": member.id, "action": "restart"])
                    item.isEnabled = !busy && ["available", "partial"].contains(member.state)
                    sub.addItem(item)
                    if member.components.filter({ $0["kind"] as? String == "server" }).count > 1 {
                        for c in member.components where c["kind"] as? String == "server" {
                            guard let cid = c["id"] as? String else { continue }
                            let title = "仅重启 " + (c["name"] as? String ?? cid)
                            let single = operationItem(title, ["project": member.id, "component": cid, "action": "restart"])
                            single.isEnabled = item.isEnabled
                            sub.addItem(single)
                        }
                    }
                }
                groupItem.submenu = sub
                menu.addItem(groupItem)
            }
        }
        menu.addItem(.separator())
        add("打开 Dashboard", #selector(openDashboard), "d")
        add("刷新状态", #selector(refreshNow), "r")
        add("退出菜单栏（后台服务继续运行）", #selector(quit), "q")
    }

    func projectItem(_ p: Project) -> NSMenuItem {
        var title = "\(p.name) — \(p.label)"
        if p.codeState == "pending" { title += " · 待更新" }
        if p.codeState == "failed" { title += " · 更新失败" }
        let m = NSMenuItem(title: title, action: #selector(clickProject(_:)), keyEquivalent: "")
        m.target = self
        m.representedObject = p.id
        if ["failed", "conflict"].contains(p.state) {
            m.image = NSImage(systemSymbolName: "exclamationmark.triangle", accessibilityDescription: "故障")
        }
        m.isEnabled = p.op == nil && !["starting", "stopping"].contains(p.state)
        return m
    }

    func operationItem(_ title: String, _ operation: [String: String]) -> NSMenuItem {
        let m = NSMenuItem(title: title, action: #selector(operate(_:)), keyEquivalent: "")
        m.target = self
        m.representedObject = operation
        return m
    }

    @objc func operate(_ sender: NSMenuItem) {
        guard let op = sender.representedObject as? [String: String], let action = op["action"] else { return }
        if action == "stop" {
            let alert = NSAlert()
            alert.messageText = "停止工作区中的所有服务？"
            alert.addButton(withTitle: "停止")
            alert.addButton(withTitle: "取消")
            if alert.runModal() != .alertFirstButtonReturn { return }
        }
        let route: String
        var body: [String: Any] = [:]
        if let group = op["group"] { route = "api/groups/\(group)/\(action)" }
        else if let project = op["project"] {
            route = "api/projects/\(project)/\(action)"
            if let component = op["component"] { body["components"] = [component] }
        } else { return }
        request("POST", route, body: body) { [weak self] code, response in
            if code != 200 {
                let alert = NSAlert()
                alert.messageText = "操作未执行"
                alert.informativeText = response?["error"] as? String ?? "无法连接控制服务"
                alert.runModal()
            }
            self?.refresh()
        }
    }

    func add(_ t: String, _ s: Selector, _ k: String) {
        let m = NSMenuItem(title: t, action: s, keyEquivalent: k)
        m.target = self
        menu.addItem(m)
    }

    @objc func clickProject(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String, let p = projects.first(where: { $0.id == id }) else { return }
        switch p.state {
        case "available" where p.canOpen, "partial" where p.canOpen, "external" where p.canOpen:
            request("POST", "api/projects/\(id)/open") { _, _ in }
        case "stopped" where !p.observeOnly:
            request("POST", "api/projects/\(id)/start", body: ["open": true]) { [weak self] _, _ in self?.refresh() }
        default:
            showDashboard(anchor: id)
        }
    }

    @objc func openDashboard() { showDashboard(anchor: nil) }

    func showDashboard(anchor: String?) {
        var s = base.absoluteString + "/"
        if let anchor { s += "#p-\(anchor)" }
        NSWorkspace.shared.open(URL(string: s)!)
    }

    @objc func refreshNow() {
        request("POST", "api/refresh") { [weak self] _, _ in self?.refresh() }
    }

    @objc func quit() { NSApp.terminate(nil) }
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
