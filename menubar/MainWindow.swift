// 主窗口：WKWebView 承载 daemon 的工作台 Dashboard。
// 只信任 http://127.0.0.1:<port> 这一个 origin；其余链接交给系统浏览器；
// 控制层不可达（ping 失败）时显示原生错误页；已加载的页面恢复后继续轮询。
// 原生桥（仅此受控页面）：lsbridge 消息 {action:"pickDirectory"|"openExternally"}。

import AppKit
import WebKit

final class MainWindowController: NSObject, NSWindowDelegate, WKNavigationDelegate, WKScriptMessageHandler {
    let window: NSWindow
    let webView: WKWebView
    let overlay: NSView
    let overlayTitle = NSTextField(labelWithString: "控制层不可用")
    let overlayDetail: NSTextField
    let retryButton = NSButton(title: "立即重试", target: nil, action: #selector(pingNow))
    let base: URL
    let portString: String
    var onLanguageChanged: (() -> Void)?
    private var language = InterfaceLanguage.english
    var reachable = true
    private var dashboardLoaded = false
    private var pendingNavigation: String?
    let session: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.connectionProxyDictionary = [:]
        c.timeoutIntervalForRequest = 3
        return URLSession(configuration: c)
    }()

    init(base: URL, portString: String) {
        self.base = base
        self.portString = portString
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1200, height: 760),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = InterfaceLanguage.english.productName
        window.minSize = NSSize(width: 960, height: 600)
        window.isReleasedWhenClosed = false // 关闭窗口只是隐藏；菜单栏仍在
        window.center()

        let config = WKWebViewConfiguration()
        webView = WKWebView(frame: NSRect(x: 0, y: 0, width: 1200, height: 760), configuration: config)
        webView.autoresizingMask = [.width, .height]

        overlay = NSView(frame: NSRect(x: 0, y: 0, width: 1200, height: 760))
        overlay.autoresizingMask = [.width, .height]
        overlay.wantsLayer = true
        overlay.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        overlayTitle.font = .boldSystemFont(ofSize: 20)
        overlayDetail = NSTextField(wrappingLabelWithString:
            "服务状态暂时未知。控制服务恢复后会自动更新。")
        overlayDetail.font = .systemFont(ofSize: 13)
        overlayDetail.textColor = .secondaryLabelColor
        overlayDetail.alignment = .center
        retryButton.bezelStyle = .rounded
        super.init()
        config.userContentController.add(self, name: "lsbridge") // 原生桥：lsbridge
        webView.navigationDelegate = self
        retryButton.target = self
        layoutOverlay()
        window.contentView?.addSubview(webView)
        window.contentView?.addSubview(overlay)
        overlay.isHidden = true
        window.delegate = self
        startPinging()
        webView.load(URLRequest(url: base))
    }

    private func layoutOverlay() {
        for v in [overlayTitle, overlayDetail, retryButton] {
            v.translatesAutoresizingMaskIntoConstraints = false
            overlay.addSubview(v)
        }
        NSLayoutConstraint.activate([
            overlayTitle.centerXAnchor.constraint(equalTo: overlay.centerXAnchor),
            overlayDetail.centerXAnchor.constraint(equalTo: overlay.centerXAnchor),
            retryButton.centerXAnchor.constraint(equalTo: overlay.centerXAnchor),
            overlayTitle.centerYAnchor.constraint(equalTo: overlay.centerYAnchor, constant: 48),
            overlayDetail.topAnchor.constraint(equalTo: overlayTitle.bottomAnchor, constant: 10),
            overlayDetail.widthAnchor.constraint(lessThanOrEqualToConstant: 460),
            retryButton.topAnchor.constraint(equalTo: overlayDetail.bottomAnchor, constant: 16),
        ])
    }

    func setLanguage(_ language: InterfaceLanguage) {
        self.language = language
        window.title = language.productName
        overlayTitle.stringValue = language.text("控制层不可用", "Daemon unavailable")
        overlayDetail.stringValue = language.text(
            "服务状态暂时未知。控制服务恢复后会自动更新。",
            "Service status is unknown. The workbench updates automatically when the daemon recovers.")
        retryButton.title = language.text("立即重试", "Retry now")
    }

    func show() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true) // 重复打开聚焦已有窗口
    }

    func evaluate(_ js: String) {
        guard dashboardLoaded else { pendingNavigation = js; return }
        webView.evaluateJavaScript(js, completionHandler: nil)
    }

    // MARK: 可达性（ping 是唯一判据；导航失败只触发一次即时 ping）

    private func startPinging() {
        Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in self?.ping() }
    }

    @objc func pingNow() { ping() }

    func ping() {
        var r = URLRequest(url: base.appendingPathComponent("api/ping"))
        r.timeoutInterval = 3
        session.dataTask(with: r) { [weak self] _, resp, _ in
            let ok = (resp as? HTTPURLResponse)?.statusCode == 200
            DispatchQueue.main.async { self?.setReachable(ok) }
        }.resume()
    }

    private func setReachable(_ ok: Bool) {
        if ok {
            if !dashboardLoaded && !webView.isLoading {
                webView.load(URLRequest(url: base))
            }
            overlay.isHidden = true
            reachable = true
        } else {
            reachable = false
            overlay.isHidden = false
        }
    }

    // MARK: WKNavigationDelegate（只允许工作台 origin）

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        guard let url = navigationAction.request.url else { decisionHandler(.cancel); return }
        if isDashboardOrigin(url) { decisionHandler(.allow); return }
        if let scheme = url.scheme?.lowercased(), ["http", "https", "mailto"].contains(scheme) {
            NSWorkspace.shared.open(url) // 管理页面外的链接默认系统浏览器打开
        }
        decisionHandler(.cancel)
    }

    private func isDashboardOrigin(_ url: URL) -> Bool {
        guard url.scheme?.lowercased() == "http",
              url.host == "127.0.0.1",
              (url.port.map(String.init) ?? "") == portString else { return false }
        return true
    }

    func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
        dashboardLoaded = false
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        dashboardLoaded = true
        if let pendingNavigation {
            self.pendingNavigation = nil
            evaluate(pendingNavigation)
        }
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        ping()
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        ping()
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        dashboardLoaded = false
        ping()
    }

    // MARK: 原生桥

    func userContentController(_ userContentController: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        guard message.frameInfo.isMainFrame,
              let origin = message.frameInfo.request.url, isDashboardOrigin(origin),
              let body = message.body as? [String: Any],
              let action = body["action"] as? String else { return }
        if action == "languageChanged" { onLanguageChanged?() }
        if action == "pickDirectory" { pickDirectory() }
        if action == "openExternally", let s = body["url"] as? String, let url = URL(string: s) {
            NSWorkspace.shared.open(url)
        }
    }

    private func pickDirectory() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.message = language.text("选择项目所在的目录", "Choose the project directory")
        DispatchQueue.main.async {
            let picked = panel.runModal() == .OK ? panel.url?.path : nil
            self.callback("lsPickDirectory", picked)
        }
    }

    /// 把单个字符串参数 JSON 编码后注入 window.<fn>(arg)；nil → window.<fn>(null)
    private func callback(_ fn: String, _ value: String?) {
        let arg: String
        if let value,
           let data = try? JSONSerialization.data(withJSONObject: [value]),
           let s = String(data: data, encoding: .utf8), s.count >= 2 {
            arg = String(s.dropFirst().dropLast())
        } else {
            arg = "null"
        }
        webView.evaluateJavaScript("window.\(fn) && window.\(fn)(\(arg))", completionHandler: nil)
    }

    // MARK: NSWindowDelegate

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil) // 关闭 ≠ 退出：只是隐藏窗口
        return false
    }
}
