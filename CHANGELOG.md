# Changelog

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
