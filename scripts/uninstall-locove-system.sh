#!/bin/bash
# 卸载 Locove／洛湾 无端口系统件（幂等；只删除本工具自己的文件，绝不碰其他 resolver/hosts/PF）。
# 用户服务数据、services/ 登记与 state 不受影响；sudo 形态同时清理系统级部分。
set -uo pipefail
UID_="$(id -un)"
GUI_UID="$(id -u)"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
RESOLVER_DIR="/etc/resolver"
RESOLVER_SUFFIX="${LOCOVE_DOMAIN_SUFFIX:-locove}"

resolve_python() {
  if [ -n "${LOCALSERVICES_PYTHON:-}" ]; then
    echo "$LOCALSERVICES_PYTHON"; return 0
  fi
  local cands="/opt/homebrew/bin/python3
/usr/local/bin/python3
/usr/bin/python3"
  local c
  while IFS= read -r c; do
    [ -x "$c" ] && { echo "$c"; return 0; }
  done <<<"$cands"
  echo "python3"
}

for job in dns proxy; do
  launchctl bootout "gui/$GUI_UID/com.mybrain.ls.locove.$job" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/com.mybrain.ls.locove.$job.plist"
done
rm -rf "$HOME/Library/Application Support/LocalServices/locove"

if [ "$(id -u)" = 0 ]; then
  launchctl bootout "system/com.mybrain.ls.locove.entry" 2>/dev/null || true
  rm -f "/Library/LaunchDaemons/com.mybrain.ls.locove.entry.plist"
  # R3：resolver 删除前做归属检查——无本工具管理标记的文件绝不动（保持他人配置原样）
  if [ -f "$RESOLVER_DIR/$RESOLVER_SUFFIX" ]; then
    (cd "$REPO" && "$(resolve_python)" -m localservices.locove_resolver uninstall \
       --dir "$RESOLVER_DIR" --suffix "$RESOLVER_SUFFIX") || {
      echo "WARN: /etc/resolver/$RESOLVER_SUFFIX 不属于本工具，保留未删（人工确认后再处理）。"
    }
  fi
  rm -rf "/Library/Application Support/Locove"
  rmdir /etc/resolver 2>/dev/null || true  # 仅当空（用户自己的 resolver 文件绝不删除）
  echo "SYSTEM PARTS REMOVED"
else
  echo "系统级部分（若已安装）需要管理员授权清理：sudo $0"
fi

# flush 解析缓存，让 .locove 立即回到未解析状态
dscacheutil -flushcache 2>/dev/null || true
killall -HUP mDNSResponder 2>/dev/null || true
echo "UNINSTALLED locove network parts (service registrations untouched)"
