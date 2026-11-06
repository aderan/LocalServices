#!/bin/bash
# Locove／洛湾 可选域名访问件安装（D2/D3：默认不随工具安装；仅在用户主动启用时运行）。
# 幂等；只创建/管理本工具自己的文件，不动用户已有 resolver/hosts/PF。
#
# 模式：
#   scripts/install-locove-system.sh --user [--port N]   # 用户级（无需 root）：
#       - launchd agent com.mybrain.ls.locove.dns    本地 DNS（127.0.0.1:5354，仅答已登记 *.locove）
#       - launchd agent com.mybrain.ls.locove.proxy  Caddy 数据面（回环 :N，缺省 5670，普通用户可绑定）
#       - 初始 Caddyfile/routes.json（daemon 之后周期收敛）
#   scripts/install-locove-system.sh --resolver        # 系统级（需 root，通常经 osascript
#       当次管理员授权调用）：/etc/resolver/locove split DNS（仅把 .locove 定向到本地 DNS；
#       归属守卫生效：他人文件拒绝覆盖，失败零残留）
#
# 历史说明：V3.4 时代的 80 端口系统转发器（com.mybrain.ls.locove.entry）自 D2 起不再
# 安装；旧安装的迁移/退役见 scripts/retire-locove-80.sh。
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
EUID_="$(id -u)"
RESOLVER_DIR="/etc/resolver"
RESOLVER_SUFFIX="${LOCOVE_DOMAIN_SUFFIX:-locove}"
DNS_PORT="${LOCOVE_DNS_PORT:-5354}"
PORT="${LOCOVE_PROXY_PORT:-5670}"

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

install_user_parts() {
  local HOME_ LA LOGS LS_HOME LOCOVE_DIR CADDY UID_
  HOME_="$HOME"
  LA="$HOME_/Library/LaunchAgents"
  LOGS="$HOME_/Library/Logs/LocalServices"
  LS_HOME="$HOME_/Library/Application Support/LocalServices"
  LOCOVE_DIR="$LS_HOME/locove"
  UID_="$(id -u)"
  # 隔离环境（测试/验收）重定向状态根与 LaunchAgents 目录；不碰生产实例
  LA="${LOCOVE_LAUNCHAGENTS:-$LA}"
  LS_HOME="${LOCALSERVICES_HOME:-$LS_HOME}"
  LOCOVE_DIR="$LS_HOME/locove"

  CADDY="${LOCOVE_CADDY:-/opt/homebrew/bin/caddy}"
  [ -x "$CADDY" ] || CADDY="$(command -v caddy 2>/dev/null || true)"
  [ -n "$CADDY" ] || { echo "ERROR: 未找到 caddy（数据面代理）。安装：brew install caddy" >&2; exit 1; }

  mkdir -p "$LA" "$LOGS" "$LOCOVE_DIR"
  # 初始/迁移配置：全新安装 → 空路由（只有 catch-all）；既有安装（如 47890→5670
  # 迁移）→ 保留 routes.json 里的路由事实，仅以目标端口重渲染 Caddyfile/payload。
  # daemon 起来后周期收敛真实路由。
  (cd "$REPO" && env LOCALSERVICES_HOME="$LS_HOME" LOCOVE_PROXY_PORT="$PORT" "$PY" - <<PYEOF
from localservices import locove
data = locove.read_routes() or {}
routes = {h: locove.Route(host=h, **{k: v for k, v in r.items() if k != "host"})
          for h, r in (data.get("routes") or {}).items()}
locove.apply_routes(routes)
PYEOF
  ) >/dev/null 2>&1 || mkdir -p "$LOCOVE_DIR"  # caddy 不可用时至少建目录

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
    <key>LOCOVE_PROXY_PORT</key><string>$PORT</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOGS/locove-proxy.log</string>
  <key>StandardErrorPath</key><string>$LOGS/locove-proxy.log</string>
</dict></plist>
PLIST

  # bootstrap 前先 bootout 旧实例（端口可能变化；等待完全退出再引导）。
  # 隔离守卫：LOCOVE_LAUNCHAGENTS 被重定向（测试/验收）时只写文件，不碰真实 launchd。
  local job
  if [ -z "${LOCOVE_LAUNCHAGENTS:-}" ]; then
    for job in dns proxy; do
      launchctl bootout "gui/$UID_/com.mybrain.ls.locove.$job" 2>/dev/null || true
      local i
      for i in $(seq 1 20); do
        launchctl print "gui/$UID_/com.mybrain.ls.locove.$job" >/dev/null 2>&1 || break
        sleep 0.25
      done
      launchctl bootstrap "gui/$UID_" "$LA/com.mybrain.ls.locove.$job.plist"
    done
    echo "USER PARTS INSTALLED: dns(:$DNS_PORT) + proxy(caddy :$PORT)"
  else
    echo "USER PARTS WRITTEN (isolated: $LA; dns :$DNS_PORT + proxy :$PORT, 未引导 launchd)"
  fi
}

install_resolver() {
  [ "$(id -u)" = 0 ] || { echo "resolver 安装需要 root（通常经启用流程的当次管理员授权调用）" >&2; exit 1; }
  # R3/S2：resolver 归属检查放在写任何系统文件之前——失败即中止，零残留
  (cd "$REPO" && "$PY" -m localservices.locove_resolver install \
     --dir "$RESOLVER_DIR" --port "$DNS_PORT" --suffix "$RESOLVER_SUFFIX") || {
    echo "ERROR: /etc/resolver/$RESOLVER_SUFFIX 归属检查未通过，系统级安装中止（未写入任何文件）。"
    exit 1
  }
  chown root:wheel "$RESOLVER_DIR/$RESOLVER_SUFFIX" 2>/dev/null || true
  echo "RESOLVER INSTALLED: /etc/resolver/$RESOLVER_SUFFIX (→ 127.0.0.1:$DNS_PORT)"
}

case "${1:-}" in
  --user)
    if [ "${2:-}" = "--port" ]; then
      PORT="${3:?--port 需要端口号}"
    fi
    install_user_parts
    ;;
  --resolver|--system)  # --system 为旧名：D2 起系统级只装 resolver（不再装 80 entry）
    install_resolver
    ;;
  *)
    cat <<'USAGE'
Locove／洛湾 可选域名访问件（默认不随工具安装）：
  --user [--port N]   用户级 DNS + Caddy 代理（回环，缺省 5670；无需 root）
  --resolver          /etc/resolver/<suffix> split DNS（需 root；归属守卫生效）
启用入口：工作台「设置 → 域名访问」或 lsctl locove-enable [--port N]。
旧 80 系统件的迁移/退役：scripts/retire-locove-80.sh
USAGE
    exit 1
    ;;
esac
