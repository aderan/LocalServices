# Changelog

## [0.5.1] - 2026-11-27

- Service readiness checks verify that the registered process owns the listening port. When the port is occupied, the workbench reports the conflict and shows the available actions.
- Test a suggested or custom replacement port from the workbench. Locove saves the new port after a successful trial. When the project must change its configuration, copy a repair prompt from the workbench.
- Codex uses Auto by default. It keeps the effective configured model when the current CLI supports it and uses the connection's recommended default when that model is incompatible. A manually pinned model stays fixed and offers an explicit **Retry with Auto** action if rejected. Locove reads the Codex configuration and applies any fallback to the current invocation.
- Fixed prompt copying in the macOS workbench and removal previews for services with port conflicts.

Build from source on macOS. See [installation, updates, and removal](https://github.com/aderan/LocalServices/blob/v0.5.1/docs/INSTALLATION.md) for requirements and checksum instructions.

## [0.2.0] - 2026-11-17

Locove's first public source preview for macOS. Use the menu bar, workbench, or `lsctl` to register, start, open, and troubleshoot local development services.

### Added

- Assign Compose services and Docker dependencies to a specific local environment. Start a stopped Colima profile from the workbench and resume services waiting for it. Project test controls can stop the entire selected Colima or OrbStack environment to test recovery.
- Press Control-Shift-L to open the workbench or Control-Shift-Space to search and open a running service. Change or disable either shortcut in desktop settings.
- Ask a configured coding agent to diagnose a service or Docker environment failure. The workbench supplies redacted evidence from status and logs and shows the agent's suggested next steps.
- For optional `.locove` domains, add a proxy exception to the active macOS network service with administrator approval. Existing exceptions are preserved.

### Improved

- Open Vite apps and dashboards that expect a loopback hostname through optional `.locove` URLs. Loopback compatibility can be set per entry, and browser pages rejected with HTTP 403 link to the relevant entry settings.
- **Start and open** activates the local domain route before opening the browser and falls back to the direct service URL if the route cannot be activated.
- Confirm before unlinking a directory from a project. The directory and its files stay on disk.

Build the app from source on your Mac. See the [installation guide](https://github.com/aderan/LocalServices/blob/v0.2.0/README.md#install) for system requirements and [installation, updates, and removal](https://github.com/aderan/LocalServices/blob/v0.2.0/docs/INSTALLATION.md) for checksum and upgrade instructions.
