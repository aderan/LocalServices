#!/bin/bash
# Locove／洛湾 无端口访问系统件安装（幂等；只创建/管理本工具自己的文件，不动用户已有 resolver/hosts/PF）。
#
# 用户级（无需 root）：
#   - launchd agent com.mybrain.ls.locove.dns    本地 DNS（127.0.0.1:5354，仅答已登记 *.locove）
#   - launchd agent com.mybrain.ls.locove.proxy  Caddy 数据面（127.0.0.1:47890，按精确 Host 路由）
#   - 初始 Caddyfile/routes.json（daemon 之后周期收敛）
# 系统级（需要一次管理员授权，root）：
#   - /etc/resolver/locove                       split DNS：只把 .locove 定向到本地 DNS
#   - /Library/LaunchDaemons/com.mybrain.ls.locove.entry.plist
#     + /Library/Application Support/Locove/libexec/locove-entry.py
#     80 端口最小特权转发器（root 绑定 127.0.0.1:80 后降权 nobody，只转发到 127.0.0.1:47890，
#      无 shell、无配置文件、无动态上游）
#
# 用法：
#   scripts/install-locove-system.sh          # 装用户级；系统级部分打印可审查的一条 sudo 命令
#   sudo scripts/install-locove-system.sh     # 一次装全（经 SUDO_USER 定位用户目录）
#   scripts/install-locove-system.sh --system # 只做系统级（内部使用；等价 sudo 形态）
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
EUID_="$(id -u)"
if [ "$EUID_" = 0 ] && [ -n "${SUDO_USER:-}" ]; then
  USER_="$SUDO_USER"
else
  USER_="$(id -un)"
fi
HOME_="$(eval echo "~$USER_")"
LA="$HOME_/Library/LaunchAgents"
LOGS="$HOME_/Library/Logs/LocalServices"
LS_HOME="$HOME_/Library/Application Support/LocalServices"
LOCOVE_DIR="$LS_HOME/locove"
RESOLVER_FILE="/etc/resolver/locove"
DAEMON_PLIST="/Library/LaunchDaemons/com.mybrain.ls.locove.entry.plist"
LIBEXEC_DIR="/Library/Application Support/Locove/libexec"
LIBEXEC="$LIBEXEC_DIR/locove-entry.py"
DNS_PORT="${LOCOVE_DNS_PORT:-5354}"
PROXY_PORT="${LOCOVE_PROXY_PORT:-47890}"

resolve_python() {
  if [ -n "${LOCALSERVICES_PYTHON:-}" ]; then
    command -v "$LOCALSERVICES_PYTHON" >/dev/null 2>&1 || { echo "LOCALSERVICES_PYTHON 不可用"; return 1; }
    echo "$LOCALSERVICES_PYTHON"; return 0
  fi
  local cands="/opt/homebrew/bin/python3
/usr/local/bin/python3
/usr/bin/python3"
  local c
  while IFS= read -r c; do
    [ -x "$c" ] && "$c" -c 'import sys; assert sys.version_info >= (3, 9)' >/dev/null 2>&1 && { echo "$c"; return 0; }
  done <<<"$cands"
  echo "未找到可用 Python ≥3.9" >&2; return 1
}
PY="$(resolve_python)"

CADDY="${LOCOVE_CADDY:-/opt/homebrew/bin/caddy}"
[ -x "$CADDY" ] || CADDY="$(command -v caddy 2>/dev/null || true)"
[ -n "$CADDY" ] || { echo "ERROR: 未找到 caddy（数据面代理）。安装：brew install caddy"; exit 1; }

install_user_parts() {
  mkdir -p "$LA" "$LOGS" "$LOCOVE_DIR"
  # 初始配置（无路由时只有 catch-all；daemon 起来后周期收敛真实路由）
  (cd "$REPO" && "$PY" - <<PYEOF
from localservices import locove
locove.apply_routes({})
PYEOF
) >/dev/null 2>&1 || (cd "$REPO" && "$PY" -c "from localservices import locove; locove.locove_dir().mkdir(parents=True, exist_ok=True)")  # caddy 不可用时至少建目录

  cat > "$LA/com.mybrain.ls.locove.dns.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.mybrain.ls.locove.dns</string>
  <key>ProgramArguments</key><array><string>$PY</string><string>-m</string><string>localservices.locove_dns</string></array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key><dict>
    <key>PYTHONDONTWRITEBYTECODE</key><string>1</string>
    <key>LOCOVE_DNS_PORT</key><string>$DNS_PORT</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOGS/locove-dns.log</string>
  <key>StandardErrorPath</key><string>$LOGS/locove-dns.log</string>
</dict></plist>
PLIST

  cat > "$LA/com.mybrain.ls.locove.proxy.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.mybrain.ls.locove.proxy</string>
  <key>ProgramArguments</key><array><string>$CADDY</string><string>run</string><string>--config</string><string>$LOCOVE_DIR/Caddyfile</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>LOCOVE_PROXY_PORT</key><string>$PROXY_PORT</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOGS/locove-proxy.log</string>
  <key>StandardErrorPath</key><string>$LOGS/locove-proxy.log</string>
</dict></plist>
PLIST

  local uid
  uid="$(id -u "$USER_")"
  for job in dns proxy; do
    launchctl bootout "gui/$uid/com.mybrain.ls.locove.$job" 2>/dev/null || true
    for _ in $(seq 1 20); do
      launchctl print "gui/$uid/com.mybrain.ls.locove.$job" >/dev/null 2>&1 || break
      sleep 0.25
    done
    launchctl bootstrap "gui/$uid" "$LA/com.mybrain.ls.locove.$job.plist"
  done
  echo "USER PARTS INSTALLED: dns(:$DNS_PORT) + proxy(caddy :$PROXY_PORT)"
}

install_system_parts() {
  [ "$(id -u)" = 0 ] || { echo "系统级安装需要 root"; exit 1; }
  mkdir -p /etc/resolver "$LIBEXEC_DIR"
  # 转发器脚本必须放 root 属主目录：以 root 执行的代码不能留在用户可写路径
  cp "$REPO/localservices/locove_entry.py" "$LIBEXEC"
  chown root:wheel "$LIBEXEC"
  chmod 0755 "$LIBEXEC"

  cat > "$DAEMON_PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.mybrain.ls.locove.entry</string>
  <key>ProgramArguments</key><array><string>/usr/bin/python3</string><string>$LIBEXEC</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>LOCOVE_ENTRY_PORT</key><string>80</string>
    <key>LOCOVE_ENTRY_TARGET</key><string>$PROXY_PORT</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>/Library/Logs/Locove/entry.log</string>
  <key>StandardErrorPath</key><string>/Library/Logs/Locove/entry.log</string>
</dict></plist>
PLIST
  chown root:wheel "$DAEMON_PLIST"
  chmod 0644 "$DAEMON_PLIST"
  mkdir -p /Library/Logs/Locove

  cat > "$RESOLVER_FILE" <<RESOLVER
# Locove／洛湾 split DNS：仅把 .locove 交给本地 DNS（由 LocalServices 管理）
nameserver 127.0.0.1
port $DNS_PORT
search_order 1
RESOLVER
  chown root:wheel "$RESOLVER_FILE"
  chmod 0644 "$RESOLVER_FILE"

  launchctl bootout "system/com.mybrain.ls.locove.entry" 2>/dev/null || true
  launchctl bootstrap "system" "$DAEMON_PLIST"
  echo "SYSTEM PARTS INSTALLED: /etc/resolver/locove + :80 entry forwarder"
}

verify() {
  echo
  echo "== 验证 =="
  (dscacheutil -q host -a name buildbase.locove || true) | sed 's/^/  /'
  echo "  curl -s --noproxy '*' -H 'Host: buildbase.locove' http://127.0.0.1/   # 80 入口（登记并启动 buildbase 后）"
  echo "  lsctl status   # daemon 状态"
}

case "${1:-}" in
  --system)
    install_system_parts
    verify
    ;;
  *)
    install_user_parts
    if [ "$EUID_" = 0 ]; then
      install_system_parts
      verify
    else
      echo
      echo "系统级部分（/etc/resolver/locove + 80 入口）需要一次管理员授权，内容可审查："
      echo "  sudo $REPO/scripts/install-locove-system.sh --system"
      echo "（只写入 /etc/resolver/locove、/Library/LaunchDaemons/com.mybrain.ls.locove.entry.plist、"
      echo "  /Library/Application Support/Locove/libexec/locove-entry.py；不动其他 resolver/hosts/PF）"
    fi
    ;;
esac
