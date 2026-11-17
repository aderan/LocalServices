import AppKit
import Carbon

// swiftc -parse-as-library menubar/GlobalShortcut.swift tests/verify_global_shortcut.swift -o /tmp/verify-shortcut

@main
struct VerifyGlobalShortcut {
    static func main() throws {
        _ = NSApplication.shared
        let suite = "locove-shortcut-test-\(UUID())"
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }

        let shortcut = GlobalShortcut(action: .mainWindow, defaults: defaults)
        let lockScreen = ShortcutKey(code: UInt32(kVK_ANSI_Q), modifiers: UInt32(controlKey | cmdKey))
        precondition(shortcut.conflict(for: lockScreen) == .lockScreen)
        let mainDefault = ShortcutKey(code: UInt32(kVK_ANSI_L), modifiers: UInt32(controlKey | shiftKey))
        precondition(shortcut.saved == mainDefault, "The main window default must be ⌃⇧L")
        let quick = GlobalShortcut(action: .quickServices, defaults: defaults)
        let quickDefault = ShortcutKey(code: UInt32(kVK_Space), modifiers: UInt32(controlKey | shiftKey))
        precondition(quick.saved == quickDefault, "The quick services default must be ⌃⇧Space")
        precondition(quick.saved != shortcut.saved)
        try quick.save(nil)
        let disabled = GlobalShortcut(action: .quickServices, defaults: defaults)
        disabled.restore()
        precondition(disabled.saved == nil && !disabled.isActive)
        let characterViewer = ShortcutKey(code: UInt32(kVK_Space), modifiers: UInt32(controlKey | cmdKey))
        precondition(shortcut.conflict(for: characterViewer) == .characterViewer)

        // Check an enabled system binding using the current machine's preferences.
        var raw: Unmanaged<CFArray>?
        if CopySymbolicHotKeys(&raw) == noErr, let entries = raw?.takeRetainedValue() as? [[String: Any]] {
            let screenshot = ShortcutKey(code: UInt32(kVK_ANSI_3), modifiers: UInt32(shiftKey | cmdKey))
            if entries.contains(where: {
                $0["kHISymbolicHotKeyEnabled"] as? Bool == true &&
                    $0["kHISymbolicHotKeyCode"] as? UInt32 == screenshot.code &&
                    $0["kHISymbolicHotKeyModifiers"] as? UInt32 == screenshot.modifiers
            }) {
                precondition(shortcut.conflict(for: screenshot) == .system)
            }
        }

        // Use an unusual chord so this check doesn't depend on the user's default binding.
        let testKey = ShortcutKey(code: UInt32(kVK_ANSI_9), modifiers: UInt32(controlKey | optionKey | shiftKey | cmdKey))
        try shortcut.save(testKey)
        precondition(shortcut.isActive)

        do {
            try shortcut.save(lockScreen)
            preconditionFailure("Lock Screen must never replace the current binding")
        } catch ShortcutConflict.lockScreen {
            precondition(shortcut.saved == testKey && shortcut.isActive)
        }

        let other = GlobalShortcut(action: .mainWindow, defaults: defaults)
        precondition(other.saved == testKey)
        do {
            try other.save(testKey)
            preconditionFailure("A second registration must fail")
        } catch ShortcutConflict.unavailable {
            precondition(shortcut.isActive)
        }

        try shortcut.save(nil)
        precondition(!shortcut.isActive && GlobalShortcut(action: .mainWindow, defaults: defaults).saved == nil)
        try other.save(testKey)
        precondition(other.isActive)
        print("global shortcut conflict, persistence, and clear checks passed")
    }
}
