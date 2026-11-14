#!/bin/bash
# Remove what install.sh added: daemon + menubar agents, all managed component jobs, app, CLI.
# Project data (each project's own data roots, databases, workspaces) is never touched.
#   --purge   also delete LocalServices' own state/logs (~/Library/Application Support/LocalServices, ~/Library/Logs/LocalServices)
set -uo pipefail
UID_="$(id -u)"
LA="$HOME/Library/LaunchAgents"
launchctl bootout "gui/$UID_/com.mybrain.localservices.menubar" 2>/dev/null
pkill -x 'Locove|MyBrainServices' 2>/dev/null
launchctl bootout "gui/$UID_/com.mybrain.localservices.daemon" 2>/dev/null
for label in $(launchctl list | awk '{print $3}' | grep '^com\.mybrain\.ls\.'); do
  echo "stopping $label"
  launchctl bootout "gui/$UID_/$label" 2>/dev/null
done
rm -f "$LA/com.mybrain.localservices.daemon.plist" "$LA/com.mybrain.localservices.menubar.plist"
rm -rf "$HOME/Applications/Locove.app" "$HOME/Applications/MyBrain Services.app"
rm -f "$HOME/.local/bin/lsctl"
# Remove only skill files this tool installed and the user did not modify (§6 契约)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
bash "$SCRIPT_DIR/uninstall-skill.sh" 2>/dev/null || true
bash "$SCRIPT_DIR/uninstall-locove-system.sh" || true
if [ "${1:-}" = "--purge" ]; then
  rm -rf "$HOME/Library/Application Support/LocalServices" "$HOME/Library/Logs/LocalServices"
fi
for _ in $(seq 1 30); do
  left=$(launchctl list | grep -c 'com\.mybrain\.l' || true)
  [ "$left" = 0 ] && break
  sleep 1
done
echo "UNINSTALLED: remaining jobs=$left (project data untouched)"
