import AppKit
import Carbon

struct ShortcutKey: Equatable, Codable {
    static let modifierSymbols: [(UInt32, String)] = [
        (UInt32(controlKey), "⌃"), (UInt32(optionKey), "⌥"),
        (UInt32(shiftKey), "⇧"), (UInt32(cmdKey), "⌘"),
    ]
    let code: UInt32
    let modifiers: UInt32

    var label: String {
        Self.modifierSymbols.filter { modifiers & $0.0 != 0 }.map(\.1).joined() + keyLabel
    }

    private static let specialKeys: [Int: String] = [
        kVK_Space: "Space", kVK_Tab: "⇥", kVK_Return: "↩", kVK_ANSI_KeypadEnter: "⌤",
        kVK_Escape: "Esc", kVK_Delete: "⌫", kVK_ForwardDelete: "⌦",
        kVK_LeftArrow: "←", kVK_RightArrow: "→", kVK_DownArrow: "↓", kVK_UpArrow: "↑",
        kVK_Home: "↖", kVK_End: "↘", kVK_PageUp: "⇞", kVK_PageDown: "⇟",
        kVK_F1: "F1", kVK_F2: "F2", kVK_F3: "F3", kVK_F4: "F4", kVK_F5: "F5",
        kVK_F6: "F6", kVK_F7: "F7", kVK_F8: "F8", kVK_F9: "F9", kVK_F10: "F10",
        kVK_F11: "F11", kVK_F12: "F12", kVK_F13: "F13", kVK_F14: "F14", kVK_F15: "F15",
        kVK_F16: "F16", kVK_F17: "F17", kVK_F18: "F18", kVK_F19: "F19", kVK_F20: "F20",
    ]

    var keyLabel: String { displayKey ?? "?" }

    // Use the current keyboard layout so the displayed key matches its physical position.
    fileprivate var displayKey: String? {
        if let special = Self.specialKeys[Int(code)] { return special }
        let source = TISCopyCurrentASCIICapableKeyboardLayoutInputSource().takeRetainedValue()
        guard let data = TISGetInputSourceProperty(source, kTISPropertyUnicodeKeyLayoutData),
              let bytes = CFDataGetBytePtr(Unmanaged<CFData>.fromOpaque(data).takeUnretainedValue()) else {
            return nil
        }
        let layout = UnsafeRawPointer(bytes).assumingMemoryBound(to: UCKeyboardLayout.self)
        var state: UInt32 = 0
        var length = 0
        var characters = [UniChar](repeating: 0, count: 8)
        let status = UCKeyTranslate(layout, UInt16(code), UInt16(kUCKeyActionDisplay), 0,
                                    UInt32(LMGetKbdType()), OptionBits(kUCKeyTranslateNoDeadKeysMask),
                                    &state, characters.count, &length, &characters)
        guard status == noErr && length > 0 else { return nil }
        let key = String(decoding: characters.prefix(length), as: UTF16.self)
        guard key.rangeOfCharacter(from: .controlCharacters) == nil else { return nil }
        return key.uppercased()
    }

    var appKitModifiers: NSEvent.ModifierFlags {
        var flags: NSEvent.ModifierFlags = []
        if modifiers & UInt32(controlKey) != 0 { flags.insert(.control) }
        if modifiers & UInt32(optionKey) != 0 { flags.insert(.option) }
        if modifiers & UInt32(shiftKey) != 0 { flags.insert(.shift) }
        if modifiers & UInt32(cmdKey) != 0 { flags.insert(.command) }
        return flags
    }
}

enum ShortcutAction: String, CaseIterable {
    case mainWindow = "mainWindowShortcut"
    case quickServices = "quickServicesShortcut"

    var defaultKey: ShortcutKey {
        switch self {
        case .mainWindow: return ShortcutKey(code: UInt32(kVK_ANSI_L), modifiers: UInt32(controlKey | shiftKey))
        case .quickServices: return ShortcutKey(code: UInt32(kVK_Space), modifiers: UInt32(controlKey | shiftKey))
        }
    }
}

enum ShortcutConflict: Error, Equatable {
    case needModifiers
    case unsupportedKey
    case lockScreen
    case characterViewer
    case action(ShortcutAction)
    case system
    case menu(String)
    case unavailable
}

final class GlobalShortcut {
    let action: ShortcutAction
    var conflictingAction: ((ShortcutKey) -> ShortcutAction?)?
    private static let signature: OSType = 0x4C4F4356 // LOCV
    private let defaults: UserDefaults
    private var registration: EventHotKeyRef?
    private var handler: EventHandlerRef?
    private static var nextID: UInt32 = 0
    private var activeID: UInt32?
    private(set) var saved: ShortcutKey?
    private(set) var startupConflict: ShortcutConflict?
    var onOpen: (() -> Void)?

    var isActive: Bool { registration != nil }

    init(action: ShortcutAction, defaults: UserDefaults = .standard) {
        self.action = action
        self.defaults = defaults
        switch defaults.object(forKey: action.rawValue) {
        case let data as Data:
            saved = try? JSONDecoder().decode(ShortcutKey.self, from: data)
        case let enabled as Bool where !enabled:
            saved = nil
        default:
            saved = action.defaultKey
        }
    }

    deinit {
        if let registration { UnregisterEventHotKey(registration) }
        if let handler { RemoveEventHandler(handler) }
    }

    func restore() {
        do {
            try save(saved)
        } catch {
            startupConflict = error as? ShortcutConflict ?? .unavailable
        }
    }

    func conflict(for shortcut: ShortcutKey) -> ShortcutConflict? {
        // Lock Screen is reserved even when absent from the symbolic hotkey list.
        if shortcut.code == UInt32(kVK_ANSI_Q) && shortcut.modifiers == UInt32(controlKey | cmdKey) {
            return .lockScreen
        }
        if shortcut.code == UInt32(kVK_Space) && shortcut.modifiers == UInt32(controlKey | cmdKey) {
            return .characterViewer
        }
        if let action = conflictingAction?(shortcut) { return .action(action) }
        if Self.isSystemShortcut(shortcut) { return .system }
        guard let key = shortcut.displayKey else { return .unsupportedKey }
        let equivalent: String
        switch key {
        case "Space": equivalent = " "
        case "⇥": equivalent = "\t"
        case "←": equivalent = String(UnicodeScalar(NSLeftArrowFunctionKey)!)
        case "→": equivalent = String(UnicodeScalar(NSRightArrowFunctionKey)!)
        case "↓": equivalent = String(UnicodeScalar(NSDownArrowFunctionKey)!)
        case "↑": equivalent = String(UnicodeScalar(NSUpArrowFunctionKey)!)
        default: equivalent = key.lowercased()
        }
        if let menu = NSApp?.mainMenu,
           let item = Self.matchingItem(in: menu, key: equivalent, flags: shortcut.appKitModifiers) {
            return .menu(item.title)
        }
        if shortcut.modifiers.nonzeroBitCount < 2 { return .needModifiers }
        return nil
    }

    private static func isSystemShortcut(_ shortcut: ShortcutKey) -> Bool {
        var list: Unmanaged<CFArray>?
        guard CopySymbolicHotKeys(&list) == noErr,
              let entries = list?.takeRetainedValue() as? [[String: Any]] else { return false }
        return entries.contains { entry in
            guard entry["kHISymbolicHotKeyEnabled"] as? Bool == true,
                  entry["kHISymbolicHotKeyCode"] as? UInt32 == shortcut.code,
                  let modifiers = entry["kHISymbolicHotKeyModifiers"] as? UInt32 else { return false }
            // The system includes an implicit Fn flag for directional keys.
            let flags = UInt32(controlKey | optionKey | shiftKey | cmdKey)
            return modifiers & flags == shortcut.modifiers
        }
    }

    private static func matchingItem(in menu: NSMenu, key: String,
                                     flags: NSEvent.ModifierFlags) -> NSMenuItem? {
        for item in menu.items {
            let equivalent = item.keyEquivalent
            var modifiers = item.keyEquivalentModifierMask
            if equivalent != equivalent.lowercased() { modifiers.insert(.shift) }
            if equivalent.lowercased() == key && modifiers == flags { return item }
            if let submenu = item.submenu, let match = matchingItem(in: submenu, key: key, flags: flags) {
                return match
            }
        }
        return nil
    }

    func save(_ shortcut: ShortcutKey?) throws {
        if let shortcut {
            if let conflict = conflict(for: shortcut) { throw conflict }
            if saved == shortcut && isActive { return }
            let data = try JSONEncoder().encode(shortcut)
            try register(shortcut)
            defaults.set(data, forKey: action.rawValue)
        } else {
            if let registration { UnregisterEventHotKey(registration) }
            registration = nil
            activeID = nil
            defaults.set(false, forKey: action.rawValue)
        }
        saved = shortcut
        startupConflict = nil
    }

    private func register(_ shortcut: ShortcutKey) throws {
        let target = GetEventDispatcherTarget()
        if handler == nil {
            var kind = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                     eventKind: UInt32(kEventHotKeyReleased))
            let status = InstallEventHandler(target, { _, event, context in
                guard let event, let context else { return OSStatus(eventNotHandledErr) }
                let owner = Unmanaged<GlobalShortcut>.fromOpaque(context).takeUnretainedValue()
                var identifier = EventHotKeyID()
                let status = GetEventParameter(event, EventParamName(kEventParamDirectObject),
                                               EventParamType(typeEventHotKeyID), nil,
                                               MemoryLayout<EventHotKeyID>.size, nil, &identifier)
                guard status == noErr && identifier.signature == GlobalShortcut.signature,
                      identifier.id == owner.activeID else { return OSStatus(eventNotHandledErr) }
                owner.onOpen?()
                return noErr
            }, 1, &kind, Unmanaged.passUnretained(self).toOpaque(), &handler)
            guard status == noErr else { throw ShortcutConflict.unavailable }
        }
        Self.nextID &+= 1
        let identifier = EventHotKeyID(signature: Self.signature, id: Self.nextID)
        var newRegistration: EventHotKeyRef?
        let status = RegisterEventHotKey(shortcut.code, shortcut.modifiers, identifier,
                                         target, OptionBits(kEventHotKeyExclusive), &newRegistration)
        guard status == noErr, let newRegistration else { throw ShortcutConflict.unavailable }
        if let registration { UnregisterEventHotKey(registration) }
        registration = newRegistration
        activeID = identifier.id
    }
}
