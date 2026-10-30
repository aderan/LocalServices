#!/bin/bash
# Install / update MyBrain Local Services for the current user (idempotent).
#   - control daemon: LaunchAgent com.mybrain.localservices.daemon (KeepAlive, runs from this repo)
#   - menu bar app:   ~/Applications/MyBrain Services.app + login LaunchAgent (no KeepAlive, so "退出" sticks)
#   - CLI:            ~/.local/bin/lsctl
# Python is resolved generically (no personal absolute path dependency):
#   LOCALSERVICES_PYTHON wins; otherwise the first python3 on PATH (then common fallbacks)
#   that is >= 3.11 (tomllib) and has no missing stdlib deps we need.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
UID_="$(id -u)"
LA="$HOME/Library/LaunchAgents"
LOGS="$HOME/Library/Logs/LocalServices"
APP="$HOME/Applications/MyBrain Services.app"
mkdir -p "$LA" "$LOGS" "$HOME/Applications" "$HOME/.local/bin"

resolve_python() {
  if [ -n "${LOCALSERVICES_PYTHON:-}" ]; then
    command -v "$LOCALSERVICES_PYTHON" 2>/dev/null || { echo "LOCALSERVICES_PYTHON 不可用: $LOCALSERVICES_PYTHON"; return 1; }
    return 0
  fi
  local cands
  cands="$(command -v python3 2>/dev/null || true)
$HOME/.pyenv/shims/python3
/opt/homebrew/bin/python3
/usr/local/bin/python3
/usr/bin/python3"
  local c
  while IFS= read -r c; do
    [ -n "$c" ] || continue
    if [ -x "$c" ] && "$c" -c 'import sys, tomllib; assert sys.version_info >= (3, 11)' >/dev/null 2>&1; then
      command -v "$c"
      return 0
    fi
  done <<<"$cands"
  echo "未找到 Python ≥3.11（含 tomllib）。请安装或用 LOCALSERVICES_PYTHON 指定解释器。" >&2
  return 1
}
PY="$(resolve_python)"

"$REPO/menubar/build.sh" >/dev/null
launchctl bootout "gui/$UID_/com.mybrain.localservices.menubar" 2>/dev/null || true
pkill -x MyBrainServices 2>/dev/null && sleep 1 || true
rm -rf "$APP"
cp -R "$REPO/menubar/build/MyBrain Services.app" "$APP"
mkdir -p "$HOME/Library/Application Support/LocalServices/bin"
cp "$REPO/menubar/build/fsevents-check" "$HOME/Library/Application Support/LocalServices/bin/fsevents-check"

cat > "$LA/com.mybrain.localservices.daemon.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.mybrain.localservices.daemon</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>-m</string><string>localservices.daemon</string></array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key><dict>
    <key>PYTHONDONTWRITEBYTECODE</key><string>1</string>
    <key>PATH</key><string>$(dirname "$PY"):$HOME/.pyenv/shims:/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$LOGS/daemon.log</string>
  <key>StandardErrorPath</key><string>$LOGS/daemon.log</string>
</dict></plist>
PLIST

cat > "$LA/com.mybrain.localservices.menubar.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.mybrain.localservices.menubar</string>
  <key>ProgramArguments</key><array><string>$APP/Contents/MacOS/MyBrainServices</string></array>
  <key>RunAtLoad</key><true/>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$LOGS/menubar.log</string>
  <key>StandardErrorPath</key><string>$LOGS/menubar.log</string>
</dict></plist>
PLIST

cat > "$HOME/.local/bin/lsctl" <<SH
#!/bin/sh
cd "$REPO" && exec "$PY" -m localservices.cli "\$@"
SH
chmod +x "$HOME/.local/bin/lsctl"

# Codex skill (localservices): safe installer — never overwrites user-modified skills
bash "$REPO/scripts/install-skill.sh" || echo "WARN: skill 安装未完成（不影响其余组件）"

for job in daemon menubar; do
  launchctl bootout "gui/$UID_/com.mybrain.localservices.$job" 2>/dev/null || true
  for _ in $(seq 1 40); do  # bootout returns before the job is fully gone
    launchctl print "gui/$UID_/com.mybrain.localservices.$job" >/dev/null 2>&1 || break
    sleep 0.25
  done
  launchctl bootstrap "gui/$UID_" "$LA/com.mybrain.localservices.$job.plist"
done

for _ in $(seq 1 20); do
  curl -sf --noproxy '*' http://127.0.0.1:47800/api/ping >/dev/null && break
  sleep 0.5
done
curl -sf --noproxy '*' http://127.0.0.1:47800/api/ping >/dev/null \
  && echo "INSTALLED: dashboard http://127.0.0.1:47800/  menubar: $APP  cli: lsctl status" \
  || { echo "INSTALL FAILED: daemon not answering; see $LOGS/daemon.log"; exit 1; }
