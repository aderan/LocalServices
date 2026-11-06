#!/bin/bash
# 卸载 Locove／洛湾 可选域名访问件（幂等；只删除本工具自己的文件，绝不碰他人 resolver/hosts/PF）。
# 业务进程、服务数据、services/ 登记与 state 不受影响。
#
# 模式：
#   --user-only       用户级：bootout + 删除 dns/proxy agent plist + locove 状态目录
#                     （routes.json/Caddyfile）。resolver 与旧 80 系统件保留。
#   --resolver-only   仅系统级 resolver（需 root；归属守卫生效：他人文件绝不动）
#   （无参数 / sudo 全形态）用户级 + 系统级全清：resolver、旧 80 entry plist/libexec、
#                     Locove libexec 目录；只删带本工具管理标记/label 的件。
set -uo pipefail
GUI_UID="$(id -u)"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
RESOLVER_DIR="/etc/resolver"
RESOLVER_SUFFIX="${LOCOVE_DOMAIN_SUFFIX:-locove}"
ENTRY_PLIST="/Library/LaunchDaemons/com.mybrain.ls.locove.entry.plist"
LIBEXEC_DIR="/Library/Application Support/Locove"

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

remove_user_parts() {
  local job LA
  LA="${LOCOVE_LAUNCHAGENTS:-$HOME/Library/LaunchAgents}"
  for job in dns proxy; do
    launchctl bootout "gui/$GUI_UID/com.mybrain.ls.locove.$job" 2>/dev/null || true
    rm -f "$LA/com.mybrain.ls.locove.$job.plist"
  done
  rm -rf "${LOCALSERVICES_HOME:-$HOME/Library/Application Support/LocalServices}/locove"
  echo "USER PARTS REMOVED (dns/proxy agents + locove 状态目录)"
}

remove_resolver() {
  [ "$(id -u)" = 0 ] || { echo "resolver 清理需要 root（经卸载流程的当次管理员授权调用）" >&2; return 1; }
  if [ -f "$RESOLVER_DIR/$RESOLVER_SUFFIX" ]; then
    (cd "$REPO" && "$(resolve_python)" -m localservices.locove_resolver uninstall \
       --dir "$RESOLVER_DIR" --suffix "$RESOLVER_SUFFIX") || {
      echo "WARN: /etc/resolver/$RESOLVER_SUFFIX 不属于本工具或不可读，保留未删（S2 守卫）。"
      return 0
    }
  fi
  rmdir "$RESOLVER_DIR" 2>/dev/null || true  # 仅当空（用户自己的 resolver 文件绝不删除）
  echo "RESOLVER REMOVED"
}

remove_legacy_entry() {
  # 旧 80 系统转发器（V3.4 时代）：label/plist/libexec 均为本工具专属命名，可安全退役。
  # 不碰其他任何 LaunchDaemon。
  [ "$(id -u)" = 0 ] || { echo "旧 80 entry 清理需要 root" >&2; return 1; }
  launchctl bootout "system/com.mybrain.ls.locove.entry" 2>/dev/null || true
  rm -f "$ENTRY_PLIST"
  rm -rf "$LIBEXEC_DIR"
  rmdir "/Library/Logs/Locove" 2>/dev/null || true
  echo "LEGACY 80 ENTRY REMOVED"
}

case "${1:-}" in
  --user-only)
    remove_user_parts
    ;;
  --resolver-only)
    remove_resolver
    ;;
  *)
    remove_user_parts
    if [ "$(id -u)" = 0 ]; then
      remove_resolver
      remove_legacy_entry
      echo "SYSTEM PARTS REMOVED"
    else
      echo "系统级残留（若有）：resolver 与旧 80 entry 需管理员授权清理：sudo $0"
    fi
    ;;
esac

dscacheutil -flushcache 2>/dev/null || true
killall -HUP mDNSResponder 2>/dev/null || true
echo "UNINSTALLED locove network parts (service registrations untouched)"
