# Locove product scope

Locove manages the repeatable steps between a local project and a running development service. It runs on the developer's Mac and keeps registrations, runtime state and logs on that machine.

| Included in 0.1.0 | Outside the 0.1.0 scope |
| --- | --- |
| Project directories, service registration, start, stop, open, status and logs | Remote machines, cloud deployment, public hosting, shared control planes and cloud sync |
| Local process ownership, port-conflict diagnosis, and an Agent-assisted repair for the port of a stopped service | Automatic port assignment, source-code edits to resolve conflicts, and stopping unrelated listeners |
| Configured local Compose services, Docker environment discovery and binding, and local development testing with Colima or OrbStack | Installing or upgrading runtimes and changing shared volumes |
| Optional Agent discovery, trial startup before registration, and local domain routing | General-purpose code repair, public DNS and TLS |
| Source installation on macOS | Prebuilt installer, iOS or Android installation, app signing and store distribution |

Locove starts the command you register, with your account's permissions. Register commands from projects you trust. Removing a registration leaves source files, project data and shared containers or volumes intact. Locove can stop the services it manages. When a separate process owns a port, Locove reports the conflict.

The daemon listens on loopback and guards local write requests. Default service URLs use the local endpoint, with no DNS or proxy installation. Your service's own bind address and network exposure are controlled by its configuration. Optional Agent calls use your chosen CLI and its provider settings.

Agent discovery proposes service definitions. Trial startup runs a proposed command locally. Agent repair shows the proposed port and command. Confirm the change to test it and update the Locove registration if the test passes.
