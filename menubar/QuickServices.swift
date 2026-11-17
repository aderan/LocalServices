import Carbon
import SwiftUI

final class QuickServicesPanel: NSPanel, NSWindowDelegate {
    private let model = QuickServicesModel()
    private var keyMonitor: Any?

    override var canBecomeKey: Bool { true }

    init() {
        super.init(contentRect: .zero, styleMask: [.titled, .fullSizeContentView, .nonactivatingPanel],
                   backing: .buffered, defer: false)
        isFloatingPanel = true
        level = .floating
        collectionBehavior = [.moveToActiveSpace, .fullScreenAuxiliary]
        titleVisibility = .hidden
        titlebarAppearsTransparent = true
        standardWindowButton(.closeButton)?.isHidden = true
        standardWindowButton(.zoomButton)?.isHidden = true
        standardWindowButton(.miniaturizeButton)?.isHidden = true
        delegate = self
    }

    func show(services: [Service], language: InterfaceLanguage, open: @escaping (String) -> Void) {
        if isVisible { dismiss(); return }
        model.query = ""
        model.selectedID = nil
        model.services = services
        contentViewController = NSHostingController(rootView: QuickServicesView(
            model: model, language: language, open: { [weak self] id in
                self?.dismiss()
                open(id)
            }))
        let visibleRows = min(max(2, services.count), 6)
        setContentSize(NSSize(width: 510, height: CGFloat(100 + 31 * visibleRows)))
        let pointer = NSEvent.mouseLocation
        if let screen = NSScreen.screens.first(where: { $0.frame.contains(pointer) }) ?? NSScreen.main {
            let area = screen.visibleFrame
            setFrameTopLeftPoint(NSPoint(x: area.midX - frame.width / 2,
                                         y: area.maxY - area.height * 0.22))
        }
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self, event.window === self else { return event }
            let modifiers: NSEvent.ModifierFlags = [.shift, .control, .option, .command]
            guard event.modifierFlags.isDisjoint(with: modifiers),
                  (firstResponder as? NSTextView)?.hasMarkedText() != true else { return event }
            switch Int(event.keyCode) {
            case kVK_Escape: dismiss()
            case kVK_UpArrow: model.moveSelection(by: -1)
            case kVK_DownArrow: model.moveSelection(by: 1)
            default: return event
            }
            return nil
        }
        makeKeyAndOrderFront(nil)
    }

    func update(_ services: [Service]) {
        if isVisible { model.services = services }
    }

    func dismiss() {
        if let keyMonitor {
            NSEvent.removeMonitor(keyMonitor)
            self.keyMonitor = nil
        }
        orderOut(nil)
    }

    func windowDidResignKey(_ notification: Notification) { dismiss() }
}

private final class QuickServicesModel: ObservableObject {
    @Published var services: [Service] = []
    @Published var query = ""
    @Published var selectedID: String?

    var results: [Service] {
        let term = query.trimmingCharacters(in: .whitespacesAndNewlines)
        return services.filter { term.isEmpty || $0.name.localizedStandardContains(term) || $0.id.localizedStandardContains(term) }
    }

    var selection: String? {
        let rows = results
        return rows.first(where: { $0.id == selectedID })?.id ?? rows.first?.id
    }

    func moveSelection(by offset: Int) {
        let rows = results
        guard let index = rows.firstIndex(where: { $0.id == selection }) else { return }
        selectedID = rows[min(max(index + offset, 0), rows.count - 1)].id
    }
}

private struct QuickServicesView: View {
    @ObservedObject var model: QuickServicesModel
    @FocusState private var searchFocused: Bool
    let language: InterfaceLanguage
    let open: (String) -> Void

    var body: some View {
        VStack(spacing: 12) {
            HStack(spacing: 10) {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary).accessibilityHidden(true)
                TextField(language.text("搜索运行中的服务…", "Search running services…"), text: $model.query)
                    .textFieldStyle(.plain)
                    .font(.title3)
                    .focused($searchFocused)
                    .onSubmit { if let id = model.selection { open(id) } }
                    .onChange(of: model.query) { _ in model.selectedID = nil }
            }
            .padding(.horizontal, 8)
            Divider()
            if model.results.isEmpty {
                Text(model.services.isEmpty
                    ? language.text("没有可以打开的运行中服务", "No running services to open")
                    : language.text("没有匹配的服务", "No matching services"))
                    .foregroundStyle(.secondary)
                    .frame(maxHeight: .infinity)
            } else {
                ScrollViewReader { scroll in
                    ScrollView {
                        VStack(spacing: 2) {
                            ForEach(model.results, id: \.id) { service in
                                let selected = model.selection == service.id
                                Button { open(service.id) } label: {
                                    HStack {
                                        Text(service.name)
                                        Spacer()
                                        if selected {
                                            Text("↩").accessibilityHidden(true)
                                        }
                                    }
                                    .padding(.horizontal, 10)
                                    .padding(.vertical, 7)
                                    .frame(maxWidth: .infinity)
                                    .contentShape(Rectangle())
                                    .background(selected ? Color.accentColor.opacity(0.17) : Color.clear,
                                                in: RoundedRectangle(cornerRadius: 6))
                                }
                                .buttonStyle(.plain)
                                .accessibilityAddTraits(selected ? .isSelected : [])
                                .id(service.id)
                            }
                        }
                    }
                    .onChange(of: model.selection) { id in
                        if let id { scroll.scrollTo(id) }
                    }
                }
            }
        }
        .padding(16)
        .ignoresSafeArea(.container, edges: .top)
        .onAppear { searchFocused = true }
    }
}
