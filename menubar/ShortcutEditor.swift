import AppKit
import Carbon
import SwiftUI

final class ShortcutRecording: ObservableObject {
    let shortcut: GlobalShortcut
    let finish: () -> Void
    @Published private(set) var candidate: ShortcutKey?
    @Published private(set) var modifiers: UInt32 = 0
    @Published private(set) var conflict: ShortcutConflict?
    private var monitor: Any?

    init(shortcut: GlobalShortcut, finish: @escaping () -> Void) {
        self.shortcut = shortcut
        self.finish = finish
    }

    deinit {
        if let monitor { NSEvent.removeMonitor(monitor) }
    }

    func start(in sheet: NSWindow) {
        guard monitor == nil else { return }
        monitor = NSEvent.addLocalMonitorForEvents(matching: [.keyDown, .flagsChanged]) {
            [weak self, weak sheet] event in
            guard let self, let sheet, sheet.isKeyWindow, event.window === sheet else { return event }
            return handle(event)
        }
    }

    func record(_ key: ShortcutKey) {
        candidate = key
        conflict = shortcut.conflict(for: key)
    }

    var canSave: Bool { candidate != nil && (conflict == nil || conflict == .unavailable) }

    func confirm() {
        if canSave, let candidate { save(candidate) }
    }

    func remove() { save(nil) }

    private func save(_ key: ShortcutKey?) {
        do {
            try shortcut.save(key)
            finish()
        } catch {
            conflict = error as? ShortcutConflict ?? .unavailable
        }
    }

    private func handle(_ event: NSEvent) -> NSEvent? {
        let flags = event.modifierFlags
        var mask: UInt32 = 0
        if flags.contains(.control) { mask |= UInt32(controlKey) }
        if flags.contains(.option) { mask |= UInt32(optionKey) }
        if flags.contains(.shift) { mask |= UInt32(shiftKey) }
        if flags.contains(.command) { mask |= UInt32(cmdKey) }
        if event.type == .flagsChanged {
            modifiers = mask
            return event
        }
        let code = Int(event.keyCode)
        // Keep keyboard navigation and button activation available inside the sheet.
        if code == kVK_Tab && (mask == 0 || mask == UInt32(shiftKey)) { return event }
        if code == kVK_Space && mask == 0 { return event }
        if event.isARepeat { return nil }
        if mask == 0 {
            switch code {
            case kVK_Escape: finish()
            case kVK_Return, kVK_ANSI_KeypadEnter: confirm()
            default: record(ShortcutKey(code: UInt32(code), modifiers: mask))
            }
        } else {
            record(ShortcutKey(code: UInt32(code), modifiers: mask))
        }
        return nil
    }
}

struct ShortcutEditor: View {
    @ObservedObject var recording: ShortcutRecording
    @Environment(\.colorScheme) private var colorScheme
    let language: InterfaceLanguage

    private var errorColor: Color {
        colorScheme == .dark ? Color(nsColor: .systemRed) : Color(red: 0.72, green: 0.14, blue: 0.12)
    }

    private var keys: [String] {
        let mask = recording.candidate?.modifiers ?? recording.modifiers
        let symbols = ShortcutKey.modifierSymbols.filter { mask & $0.0 != 0 }.map(\.1)
        guard let candidate = recording.candidate else { return symbols }
        return symbols + [candidate.code == UInt32(kVK_Space) ? language.text("空格", "Space") : candidate.keyLabel]
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(recording.shortcut.action.title(in: language)).font(.headline)
            Text(language.text("按下组合键，然后按回车保存。", "Press a key combination, then press Return to save."))
                .foregroundStyle(.secondary)
            HStack(spacing: 6) {
                if keys.isEmpty {
                    Text(language.text("等待输入…", "Waiting for shortcut…"))
                        .foregroundStyle(.secondary)
                } else {
                    ForEach(Array(keys.enumerated()), id: \.offset) { _, key in
                        Text(key)
                            .font(.system(.body, design: .monospaced).weight(.medium))
                            .padding(.horizontal, 9)
                            .padding(.vertical, 4)
                            .background(.quaternary, in: RoundedRectangle(cornerRadius: 5))
                    }
                }
            }
            .frame(maxWidth: .infinity, minHeight: 42)
            .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 6))
            .overlay {
                RoundedRectangle(cornerRadius: 6)
                    .strokeBorder(recording.conflict == nil ? Color.accentColor : errorColor, lineWidth: 1)
            }
            .accessibilityElement(children: .combine)
            .accessibilityIdentifier("shortcut-preview")

            ZStack(alignment: .leading) {
                Color.clear.frame(height: 28)
                if let conflict = recording.conflict {
                    Label(conflict.message(in: language), systemImage: "exclamationmark.triangle.fill")
                        .foregroundStyle(errorColor)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityIdentifier("shortcut-conflict")
                }
            }

            HStack {
                if recording.shortcut.saved != nil {
                    Button(language.text("移除快捷键", "Remove shortcut"), action: recording.remove)
                }
                Spacer()
                Button(language.text("取消", "Cancel"), action: recording.finish)
                Button(language.text("保存", "Save"), action: recording.confirm)
                    .buttonStyle(.borderedProminent)
                    .disabled(!recording.canSave)
            }
        }
        .padding(20)
        .frame(width: 440)
    }
}

extension ShortcutConflict {
    func message(in language: InterfaceLanguage) -> String {
        switch self {
        case .needModifiers:
            return language.text("请选择至少两个修饰键。", "Choose at least two modifier keys.")
        case .unsupportedKey:
            return language.text("这个键无法用于全局快捷键，请试试其他键。", "This key can’t be used for a global shortcut. Try another key.")
        case .lockScreen:
            return language.text("⌃⌘Q 已被 macOS 用于锁定屏幕。", "macOS uses ⌃⌘Q to lock the screen.")
        case .characterViewer:
            return language.text("⌃⌘空格已被 macOS 用于打开字符检视器。", "macOS uses ⌃⌘Space to open the Character Viewer.")
        case .action(let action):
            let title = action.title(in: language)
            return language.text("这个组合已被「\(title)」占用。", "This combination is already used by “\(title)”.")
        case .system:
            return language.text("这个组合已被 macOS 键盘快捷键占用。", "This combination is already assigned in macOS Keyboard Shortcuts.")
        case .menu(let title):
            return language.text("这个组合已被菜单命令「\(title)」占用。", "This combination is already used by the “\(title)” menu command.")
        case .unavailable:
            return language.text("无法注册此组合，可能已被其他应用占用。", "Could not register this combination. Another app may be using it.")
        }
    }
}

extension ShortcutAction {
    func title(in language: InterfaceLanguage) -> String {
        switch self {
        case .mainWindow: return language.text("打开主窗口", "Open main window")
        case .quickServices: return language.text("快速打开运行中的服务", "Quick open running services")
        }
    }
}
