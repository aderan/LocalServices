#!/bin/bash
# 刷新 root 属主的 80 端口转发器副本并重启 LaunchDaemon（需 root；由安装器/管理员授权调用）。
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LIBEXEC="/Library/Application Support/Locove/libexec/locove-entry.py"
cp "$REPO/localservices/locove_entry.py" "$LIBEXEC"
chown root:wheel "$LIBEXEC"
chmod 0755 "$LIBEXEC"
launchctl bootout system/com.mybrain.ls.locove.entry 2>/dev/null || true
launchctl bootstrap system /Library/LaunchDaemons/com.mybrain.ls.locove.entry.plist
echo "REFRESHED: $LIBEXEC"
