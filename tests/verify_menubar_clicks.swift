// Run with `swift tests/verify_menubar_clicks.swift <pid>` in a macOS GUI session
// with Accessibility permission. Use a test copy of the built app with
// CFBundleIdentifier com.mybrain.localservices.menubar.guitest. The test quits it.
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

func dismissMenu() {
    let escape: CGKeyCode = 53
    CGEvent(keyboardEventSource: nil, virtualKey: escape, keyDown: true)?.post(tap: .cghidEventTap)
    CGEvent(keyboardEventSource: nil, virtualKey: escape, keyDown: false)?.post(tap: .cghidEventTap)
    usleep(350_000)
}

func visibleMenuItems(of item: AXUIElement) -> [AXUIElement] {
    children(item).flatMap(children).filter { !frame($0).isEmpty }
}

func title(_ element: AXUIElement) -> String {
    attribute(element, kAXTitleAttribute) ?? ""
}

check(AXIsProcessTrusted() && CGPreflightPostEventAccess(), "Accessibility permission is required")
guard CommandLine.arguments.count == 2,
      let pid = pid_t(CommandLine.arguments[1]),
      let runningApp = NSRunningApplication(processIdentifier: pid),
      runningApp.bundleIdentifier == "com.mybrain.localservices.menubar.guitest" else {
    fputs("Provide the PID of the isolated menu bar test app\n", stderr)
    exit(1)
}

let app = AXUIElementCreateApplication(pid)
guard let menuBar: AXUIElement = attribute(app, kAXExtrasMenuBarAttribute),
      let item = children(menuBar).first else {
    fputs("Test app has no visible menu bar icon\n", stderr)
    exit(1)
}
let windows: [AXUIElement] = attribute(app, kAXWindowsAttribute) ?? []
if let window = windows.first, let closeButton: AXUIElement = attribute(window, kAXCloseButtonAttribute) {
    check(AXUIElementPerformAction(closeButton, kAXPressAction as CFString) == .success, "Close the main window")
}
let closed: [AXUIElement] = attribute(app, kAXWindowsAttribute) ?? []
check(!closed.contains { title($0) == "Locove／洛湾" }, "Main window must be closed before selecting Open")

click(item, button: .right)
let quickActions = visibleMenuItems(of: item).filter { !title($0).isEmpty }
let quickActionTitles = ["打开主窗口", "退出菜单栏（后台服务继续运行）"]
check(quickActions.map(title) == quickActionTitles, "Right click should show only Open and Quit")
click(quickActions[0])
let reopened: [AXUIElement] = attribute(app, kAXWindowsAttribute) ?? []
check(reopened.contains { title($0) == "Locove／洛湾" }, "Open should reopen the main window")

click(item)
check(visibleMenuItems(of: item).contains { title($0) == "刷新状态" }, "Left click should show the full menu")
dismissMenu()

click(item, flags: .maskControl)
check(visibleMenuItems(of: item).map(title).filter { !$0.isEmpty } == quickActionTitles,
      "Control-click should show the same quick menu")
dismissMenu()

click(item, button: .right)
guard let quit = visibleMenuItems(of: item).first(where: { title($0).hasPrefix("退出菜单栏") }) else {
    check(false, "Right click should show Quit after switching menus")
    exit(1)
}
click(quit)
let quitDeadline = Date().addingTimeInterval(5)
while kill(pid, 0) == 0 && Date() < quitDeadline {
    usleep(50_000)
}
check(kill(pid, 0) == -1 && errno == ESRCH, "Quit should terminate the menu bar app")
print("PASS: right-click Open and Quit, left-click full menu, Control-click quick menu")
