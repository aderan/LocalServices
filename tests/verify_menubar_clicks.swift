// Run with `swift tests/verify_menubar_clicks.swift <pid>` in a macOS GUI session
// with Accessibility permission. Use a test copy of the built app with
// CFBundleIdentifier com.mybrain.localservices.menubar.guitest. The test quits it.
// Installed app: pass <pid> <screenshot.png> --installed [failed-service-id].
// The installed mode only opens UI and reads accessibility state.
import AppKit
import ApplicationServices

func check(_ condition: Bool, _ message: String) {
    guard condition else {
        fputs("FAIL: \(message)\n", stderr)
        exit(1)
    }
}

func attribute<T>(_ element: AXUIElement, _ name: String) -> T? {
    var value: CFTypeRef?
    AXUIElementCopyAttributeValue(element, name as CFString, &value)
    return value as? T
}

func children(_ element: AXUIElement) -> [AXUIElement] {
    attribute(element, kAXChildrenAttribute) ?? []
}

func frame(_ element: AXUIElement) -> CGRect {
    var origin = CGPoint.zero
    var size = CGSize.zero
    if let position: AXValue = attribute(element, kAXPositionAttribute) {
        AXValueGetValue(position, .cgPoint, &origin)
    }
    if let dimensions: AXValue = attribute(element, kAXSizeAttribute) {
        AXValueGetValue(dimensions, .cgSize, &size)
    }
    return CGRect(origin: origin, size: size)
}

func click(_ element: AXUIElement, button: CGMouseButton = .left, flags: CGEventFlags = []) {
    let bounds = frame(element)
    check(!bounds.isEmpty, "Click target must be visible")
    let point = CGPoint(x: bounds.midX, y: bounds.midY)
    let down: CGEventType = button == .right ? .rightMouseDown : .leftMouseDown
    let up: CGEventType = button == .right ? .rightMouseUp : .leftMouseUp
    CGEvent(mouseEventSource: nil, mouseType: .mouseMoved, mouseCursorPosition: point, mouseButton: button)?.post(tap: .cghidEventTap)
    usleep(80_000)
    let press = CGEvent(mouseEventSource: nil, mouseType: down, mouseCursorPosition: point, mouseButton: button)
    press?.flags = flags
    press?.post(tap: .cghidEventTap)
    usleep(50_000)
    let release = CGEvent(mouseEventSource: nil, mouseType: up, mouseCursorPosition: point, mouseButton: button)
    release?.flags = flags
    release?.post(tap: .cghidEventTap)
    usleep(350_000)
}

func pressKey(_ code: CGKeyCode) {
    CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: true)?.post(tap: .cghidEventTap)
    CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: false)?.post(tap: .cghidEventTap)
    usleep(350_000)
}

func typeText(_ text: String) {
    let characters = Array(text.utf16)
    for down in [true, false] {
        let event = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: down)
        event?.keyboardSetUnicodeString(stringLength: characters.count, unicodeString: characters)
        event?.post(tap: .cghidEventTap)
    }
}

func descendants(_ element: AXUIElement, depth: Int = 9) -> [AXUIElement] {
    guard depth > 0 else { return [] }
    return children(element).flatMap { [$0] + descendants($0, depth: depth - 1) }
}

func title(_ element: AXUIElement) -> String {
    attribute(element, kAXTitleAttribute) ?? ""
}

check(AXIsProcessTrusted() && CGPreflightPostEventAccess(), "Accessibility permission is required")
let verifyLanguage = CommandLine.arguments.contains("--language")
let installed = verifyLanguage || CommandLine.arguments.contains("--installed")
let bundle = installed ? "com.mybrain.localservices.menubar" : "com.mybrain.localservices.menubar.guitest"
guard CommandLine.arguments.count >= 2,
      let pid = pid_t(CommandLine.arguments[1]),
      let runningApp = NSRunningApplication(processIdentifier: pid),
      runningApp.bundleIdentifier == bundle else {
    fputs("Provide the PID of the expected menu bar app\n", stderr)
    exit(1)
}

let app = AXUIElementCreateApplication(pid)
guard let menuBar: AXUIElement = attribute(app, kAXExtrasMenuBarAttribute),
      let item = children(menuBar).first else {
    fputs("Test app has no visible menu bar icon\n", stderr)
    exit(1)
}

func control(_ id: String) -> AXUIElement? {
    (descendants(item) + descendants(app)).first {
        (attribute($0, kAXIdentifierAttribute) as String?) == id && !frame($0).isEmpty
    }
}

func waitFor(_ message: String, _ condition: () -> Bool) {
    for _ in 0..<30 {
        if condition() { return }
        usleep(100_000)
    }
    let candidates = descendants(item) + descendants(app)
    for element in candidates where (attribute(element, kAXIdentifierAttribute) as String?)?.hasPrefix("locove-") == true
        || (attribute(element, kAXRoleAttribute) as String?) == "AXPopover" {
        print(title(element), attribute(element, kAXIdentifierAttribute) as String? ?? "-", frame(element))
    }
    check(false, message)
}

if verifyLanguage {
    func windowNamed(_ name: String) -> Bool {
        (attribute(app, kAXWindowsAttribute) as [AXUIElement]? ?? []).contains { title($0) == name }
    }
    click(item)
    waitFor("Open the settings panel") { control("locove-settings") != nil }
    click(control("locove-settings")!)
    func buttonNamed(_ name: String) -> AXUIElement? {
        descendants(app).first {
            let role: String = attribute($0, kAXRoleAttribute) ?? ""
            return title($0) == name && (role == "AXButton" || role == "AXCheckBox")
        }
    }
    for (button, name) in [("English", "Locove"), ("简体中文", "洛湾")] {
        waitFor("Language button must be available") { buttonNamed(button) != nil }
        click(buttonNamed(button)!)
        waitFor("Native window must be titled \(name)") { windowNamed(name) }
        click(item)
        waitFor("Panel brand must match \(name)") {
            guard let brand = control("locove-brand") else { return false }
            return title(brand) == name || (attribute(brand, kAXValueAttribute) as String?) == name
        }
        pressKey(53)
    }
    print("PASS: native title and panel brand switch between Locove and 洛湾")
    exit(0)
}

for (button, flags): (CGMouseButton, CGEventFlags) in [
    (.left, []), (.right, []), (.left, .maskControl)
] {
    print("CLICK", button.rawValue, flags.rawValue, frame(item))
    click(item, button: button, flags: flags)
    waitFor("Each click must open the same panel (\(button.rawValue), \(flags.rawValue))") {
        control("locove-filter") != nil && control("locove-refresh") != nil
            && control("locove-main") != nil && control("locove-settings") != nil
            && control("locove-quit") != nil
    }
    pressKey(53)
    waitFor("Escape must dismiss the panel") { control("locove-filter") == nil }
}

click(item)
waitFor("The panel must reopen") { control("locove-filter") != nil }
if CommandLine.arguments.count >= 3 {
    guard let panel = control("locove-panel") else { fatalError("Panel frame unavailable") }
    let bounds = frame(panel).integral
    let capture = Process()
    capture.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
    capture.arguments = ["-x", "-R\(Int(bounds.minX)),\(Int(bounds.minY)),\(Int(bounds.width)),\(Int(bounds.height))",
                         CommandLine.arguments[2]]
    try capture.run()
    capture.waitUntilExit()
    check(capture.terminationStatus == 0, "Capture the real menu bar panel")
}
if installed {
    if control("locove-service-shicha") != nil && control("locove-service-browsercapture") != nil {
        click(control("locove-filter")!)
        typeText("BrowserCapture")
        waitFor("The filter must keep matching rows only") {
            control("locove-service-browsercapture") != nil && control("locove-service-shicha") == nil
        }
        waitFor("The filter must provide a clear button") { control("locove-clear") != nil }
        click(control("locove-clear")!)
        waitFor("Clearing the filter must restore the list") { control("locove-service-shicha") != nil }
    }
    if let group = control("locove-group-ui-atlas") {
        click(group)
        waitFor("A multi-service project must expand") { control("locove-service-ui-atlas") != nil }
        click(group)
        waitFor("The project must collapse") { control("locove-service-ui-atlas") == nil }
    }
    click(control("locove-settings")!)
    waitFor("Settings must display in the main window") {
        descendants(app).contains { title($0).contains("域名访问") || (attribute($0, kAXValueAttribute) as String? ?? "").contains("域名访问") }
    }
    if CommandLine.arguments.count == 5 {
        let serviceID = CommandLine.arguments[4]
        let search = descendants(app).first { title($0) == "全局搜索" }
        if let search {
            AXUIElementSetAttributeValue(search, kAXValueAttribute as CFString, "old-filter" as CFString)
        }
        click(item)
        waitFor("The diagnostic service must appear") { control("locove-service-\(serviceID)") != nil }
        click(control("locove-service-\(serviceID)")!)
        waitFor("The selected service diagnosis must receive focus") {
            guard let focus: AXUIElement = attribute(app, kAXFocusedUIElementAttribute) else { return false }
            return (attribute(focus, "AXDOMIdentifier") as String?) == "reason-\(serviceID)"
        }
    }
    print("PASS: installed shared left/right/Control panel, Escape, filtering, project expansion, settings, focused diagnosis")
    exit(0)
}
waitFor("The fixture project must be available") { control("locove-group-w3fix") != nil }
click(control("locove-group-w3fix")!)
waitFor("Expansion must show the failed service action") { control("locove-service-w3-bad") != nil }
click(control("locove-service-w3-bad")!)
waitFor("The diagnosis action must show the main window") {
    (attribute(app, kAXWindowsAttribute) as [AXUIElement]? ?? []).contains { ["洛湾", "Locove"].contains(title($0)) }
}
click(item, button: .right)
waitFor("Quit must be in the shared panel") { control("locove-quit") != nil }
click(control("locove-quit")!)
waitFor("Quit must terminate the menu bar app") { kill(pid, 0) == -1 && errno == ESRCH }
print("PASS: shared left/right/Control panel, Escape, fixture expansion, diagnosis, quit")
