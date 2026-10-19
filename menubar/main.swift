// MyBrain Services menu bar: a thin client of the LocalServices daemon (same /api/status as the dashboard).
import AppKit

let port = ProcessInfo.processInfo.environment["LOCALSERVICES_PORT"] ?? "47800"
let base = URL(string: "http://127.0.0.1:\(port)")!
let tokenPath = (NSHomeDirectory() as NSString).appendingPathComponent("Library/Application Support/LocalServices/token")

struct Project {
    let id: String, name: String, state: String, label: String, codeState: String, codeLabel: String
    let observeOnly: Bool, canOpen: Bool, op: String?
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
                                   canOpen: d["can_open"] as? Bool ?? false, op: d["op"] as? String)
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
            for p in projects {
                var title = "\(p.name) — \(p.label)"
                if p.codeState == "pending" { title += " · 待更新" }
                if p.codeState == "failed" { title += " · 更新失败" }
                let m = NSMenuItem(title: title, action: #selector(clickProject(_:)), keyEquivalent: "")
                m.target = self
                m.representedObject = p.id
                if p.op != nil || p.state == "starting" || p.state == "stopping" { m.isEnabled = false }
                if p.state == "failed" || p.state == "conflict" {
                    m.image = NSImage(systemSymbolName: "exclamationmark.triangle", accessibilityDescription: "故障")
                }
                menu.addItem(m)
            }
        }
        menu.addItem(.separator())
        add("打开 Dashboard", #selector(openDashboard), "d")
        add("刷新状态", #selector(refreshNow), "r")
        add("退出菜单栏（后台服务继续运行）", #selector(quit), "q")
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
