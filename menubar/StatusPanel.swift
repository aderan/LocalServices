import AppKit
import SwiftUI

final class StatusPanelWindow: NSPanel {
    override var canBecomeKey: Bool { true }
}

struct StatusPanelSection {
    let title: String
    let projects: [(WorkspaceProject, [Service])]
}

final class StatusPanelModel: ObservableObject {
    @Published var language = InterfaceLanguage.chinese
    @Published var sections: [StatusPanelSection] = []
    @Published var connection = "连接中…"
    @Published var connected = false
    @Published var warnings: [String] = []
}

struct StatusPanel: View {
    static let size = NSSize(width: 410, height: 556)
    @ObservedObject var model: StatusPanelModel
    let app: App
    @Environment(\.colorScheme) private var colorScheme
    @State private var query = ""
    @State private var expanded: Set<String> = []

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            VStack(spacing: 10) {
                HStack(spacing: 7) {
                    Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                    TextField(model.language.text("快速过滤服务…", "Filter services…"), text: $query)
                        .textFieldStyle(.plain)
                        .accessibilityIdentifier("locove-filter")
                    if !query.isEmpty {
                        icon("xmark.circle.fill", model.language.text("清空搜索", "Clear search"), "locove-clear") { query = "" }
                    }
                }
                .padding(.horizontal, 9)
                .frame(height: 32)
                .background(.primary.opacity(0.055), in: RoundedRectangle(cornerRadius: 8))
                ScrollView(showsIndicators: false) {
                    VStack(alignment: .leading, spacing: 16) {
                        ForEach(model.warnings, id: \.self) { warning in
                            Label(warning, systemImage: "exclamationmark.triangle")
                                .font(.caption).foregroundStyle(Color.orange)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                        if !model.connected {
                            Text(model.connection).foregroundStyle(.secondary).padding(8)
                        } else if filtered.isEmpty {
                            Text(query.isEmpty ? model.language.text("还没有已登记的服务", "No registered services") : model.language.text("没有匹配的项目或服务", "No matching projects or services"))
                                .foregroundStyle(.secondary).padding(8)
                        } else {
                            ForEach(filtered, id: \.title) { section in
                                VStack(alignment: .leading, spacing: 6) {
                                    Text(section.title).font(.system(size: 12, weight: .medium))
                                        .foregroundStyle(.secondary).padding(.horizontal, 7)
                                    VStack(spacing: 0) {
                                        ForEach(Array(section.projects.enumerated()), id: \.element.0.id) { index, entry in
                                            if index > 0 { Divider().padding(.leading, 10) }
                                            project(entry.0, entry.1)
                                        }
                                    }
                                    .background(Color(nsColor: .controlBackgroundColor), in: RoundedRectangle(cornerRadius: 10))
                                    .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(.primary.opacity(0.09)))
                                }
                            }
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.bottom, 1)
                }
            }
            .padding(12)
            Divider()
            HStack(spacing: 8) {
                Button(model.language.text("添加项目…", "Add project…")) { app.newProject() }
                    .accessibilityIdentifier("locove-add")
                Spacer()
                Button(model.language.text("在浏览器中打开", "Open in browser")) { app.openDashboard() }
                    .accessibilityIdentifier("locove-browser")
                Button(model.language.text("退出", "Quit")) { app.quit() }
                    .accessibilityIdentifier("locove-quit")
                    .help(model.language.text("退出菜单栏，后台服务继续运行", "Quit the menu bar app. Services keep running."))
            }
            .buttonStyle(.borderless)
            .padding(.horizontal, 16)
            .frame(height: 42)
        }
        .frame(width: Self.size.width, height: Self.size.height)
        .background(colorScheme == .dark
                    ? Color(red: 0.15, green: 0.16, blue: 0.18)
                    : Color(red: 0.96, green: 0.97, blue: 0.98))
        .clipShape(RoundedRectangle(cornerRadius: 13))
        .overlay(RoundedRectangle(cornerRadius: 13).strokeBorder(.primary.opacity(0.12)))
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("locove-panel")
        .onExitCommand { app.closeStatusPanel() }
    }

    private var header: some View {
        HStack(spacing: 8) {
            if let mark = NSImage(named: "LocoveStatus") {
                Image(nsImage: mark).resizable().scaledToFit().frame(width: 32, height: 32)
                    .accessibilityHidden(true)
            }
            VStack(alignment: .leading, spacing: 3) {
                Text(model.language.productName).accessibilityIdentifier("locove-brand").font(.system(size: 15, weight: .semibold))
                HStack(spacing: 5) {
                    dot(model.connected ? .green : .secondary)
                    Text(model.connection).font(.caption).foregroundStyle(.secondary)
                }
            }
            Spacer()
            icon("arrow.clockwise", model.language.text("刷新状态", "Refresh status"), "locove-refresh", action: app.refreshNow)
            icon("macwindow", model.language.text("打开主窗口", "Open main window"), "locove-main", action: app.showMainWindow)
            icon("gearshape", model.language.text("设置", "Settings"), "locove-settings", action: app.showSettings)
        }
        .padding(.horizontal, 13)
        .frame(height: 60)
    }

    private var filtered: [StatusPanelSection] {
        let term = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !term.isEmpty else { return model.sections }
        return model.sections.compactMap { section in
            let projects = section.projects.compactMap { project, services -> (WorkspaceProject, [Service])? in
                let matches = project.name.localizedCaseInsensitiveContains(term) ? services : services.filter {
                    $0.name.localizedCaseInsensitiveContains(term) || $0.id.localizedCaseInsensitiveContains(term)
                }
                return matches.isEmpty ? nil : (project, matches)
            }
            return projects.isEmpty ? nil : StatusPanelSection(title: section.title, projects: projects)
        }
    }

    @ViewBuilder private func project(_ project: WorkspaceProject, _ services: [Service]) -> some View {
        let searching = !query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        if services.count == 1, let service = services.first, project.serviceIds.count == 1 {
            serviceRow(service, project: project)
        } else {
            let isExpanded = searching || expanded.contains(project.id)
            Button {
                if isExpanded { expanded.remove(project.id) } else { expanded.insert(project.id) }
            } label: {
                HStack(spacing: 8) {
                    dot(color(for: services))
                    Text(project.name).lineLimit(1)
                    Spacer()
                    Text(app.projectSummary(services)).foregroundStyle(.secondary).font(.caption)
                    Image(systemName: isExpanded ? "chevron.down" : "chevron.right").font(.caption)
                }
                .frame(maxWidth: .infinity, minHeight: 36)
                .padding(.horizontal, 10)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(searching)
            .accessibilityIdentifier("locove-group-\(project.id)")
            if isExpanded {
                ForEach(services.sorted(by: { $0.name < $1.name }), id: \.id) { service in
                    Divider().padding(.leading, 20)
                    serviceRow(service).padding(.leading, 10)
                }
            }
        }
    }

    private func serviceRow(_ service: Service, project: WorkspaceProject? = nil) -> some View {
        HStack(spacing: 8) {
            dot(color(for: [service]))
            VStack(alignment: .leading, spacing: 3) {
                Text(service.name).font(.system(size: 13, weight: .medium)).lineLimit(1)
                if let project, project.name != service.name {
                    Text(project.name).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
                if service.needsAttention && !service.reason.isEmpty {
                    Text(service.reason).font(.caption).foregroundStyle(color(for: [service])).lineLimit(1)
                        .help(service.reason)
                }
            }
            Spacer(minLength: 4)
            VStack(alignment: .trailing, spacing: 2) {
                Text(model.language.serviceLabel(service)).foregroundStyle(color(for: [service]))
                if ["pending", "failed"].contains(service.codeState) {
                    Text(model.language.text(service.codeLabel, service.codeState == "failed" ? "Update failed" : "Update pending")).foregroundStyle(.secondary)
                }
            }
            .font(.caption).lineLimit(1)
            Button(app.primaryTitle(service)) { app.activateService(service.id) }
                .buttonStyle(StatusActionStyle(prominent: service.canOpen && service.running,
                                               warning: service.needsAttention,
                                               statusColor: color(for: [service])))
                .controlSize(.small)
                .disabled(service.busy || service.state == "unknown")
                .accessibilityIdentifier("locove-service-\(service.id)")
            Menu {
                if service.canTakeover {
                    Button(model.language.text("接管外部实例…", "Take over external instance…")) { app.performOperation(service.id, "takeover") }.disabled(service.busy)
                }
                if service.intent == "running" && !service.observeOnly && service.state != "external" {
                    Button(model.language.text("停止", "Stop")) { app.performOperation(service.id, "stop") }.disabled(service.busy)
                }
                if ["available", "partial"].contains(service.state) && !service.observeOnly {
                    Button(model.language.text("重启", "Restart")) { app.performOperation(service.id, "restart") }.disabled(service.busy)
                }
                Button(model.language.text("日志（工作台）", "Logs (workbench)")) { app.performOperation(service.id, "logs") }
            } label: {
                Image(systemName: "ellipsis").frame(width: 22)
            }
            .menuIndicator(.hidden)
            .menuStyle(.borderlessButton)
            .fixedSize()
            .accessibilityLabel(model.language.text("更多操作", "More actions"))
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 7)
        .frame(minHeight: 38)
    }

    private func dot(_ color: Color) -> some View {
        Circle().fill(color).frame(width: 7, height: 7).accessibilityHidden(true)
    }

    private func color(for services: [Service]) -> Color {
        if services.contains(where: \.needsAttention) {
            return colorScheme == .dark ? Color(red: 1, green: 0.48, blue: 0.44) : Color(red: 0.71, green: 0.14, blue: 0.09)
        }
        if services.contains(where: { $0.state == "partial" }) {
            return colorScheme == .dark ? Color(red: 0.95, green: 0.76, blue: 0.31) : Color(red: 0.53, green: 0.34, blue: 0)
        }
        if services.contains(where: \.busy) { return .accentColor }
        if services.contains(where: \.running) {
            return colorScheme == .dark ? Color(red: 0.36, green: 0.82, blue: 0.48) : Color(red: 0.09, green: 0.46, blue: 0.28)
        }
        return .secondary
    }

    private func icon(_ symbol: String, _ label: String, _ id: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol).frame(width: 25, height: 25).contentShape(Rectangle())
        }
        .buttonStyle(.borderless)
        .accessibilityLabel(label)
        .accessibilityIdentifier(id)
        .help(label)
    }
}

private struct StatusActionStyle: ButtonStyle {
    @Environment(\.isEnabled) private var enabled
    let prominent: Bool
    let warning: Bool
    let statusColor: Color

    func makeBody(configuration: Configuration) -> some View {
        let color: Color = warning ? statusColor : Color(red: 0.09, green: 0.35, blue: 0.81)
        configuration.label
            .font(.system(size: 12, weight: .medium))
            .foregroundStyle(prominent ? Color.white : warning ? color : .primary)
            .padding(.horizontal, 8)
            .frame(minHeight: 24)
            .background(prominent ? color : color.opacity(warning ? 0.09 : 0.06),
                        in: RoundedRectangle(cornerRadius: 6))
            .opacity(enabled ? (configuration.isPressed ? 0.75 : 1) : 0.45)
    }
}
