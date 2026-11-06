#!/bin/bash
# Locove／洛湾 旧 80 系统件退役/迁移（D4，2026-11-06）。
#
# 背景：V3.4 时代为无端口访问安装了 root 80 转发器；D2/D3 起业务域名走回环高端口
# （缺省 5670，普通用户 Caddy 直接监听），80 转发器不再属于交付面。
#
# 本脚本只处理确认归属本工具的 80 系统件（label com.mybrain.ls.locove.entry、
# 其 root 属主 plist 与 libexec 副本），不碰业务进程、数据、登记、用户 agents、
# resolver（resolver 默认保留：5670 模式仍需要 .locove 解析）。
#
# 用法：
#   scripts/retire-locove-80.sh            # 盘点 + 计划（dry-run，零改动）
#   sudo scripts/retire-locove-80.sh --apply              # 退役 80 件（保留 resolver）
#   sudo scripts/retire-locove-80.sh --apply --remove-resolver   # 连 resolver 一起移除
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
ENTRY_LABEL="com.mybrain.ls.locove.entry"
ENTRY_PLIST="/Library/LaunchDaemons/${ENTRY_LABEL}.plist"
LIBEXEC_DIR="/Library/Application Support/Locove"
RESOLVER="/etc/resolver/${LOCOVE_DOMAIN_SUFFIX:-locove}"
RESOLVED=0

entry_plist_present() { [ -f "$ENTRY_PLIST" ]; }
entry_loaded() { launchctl print "system/$ENTRY_LABEL" >/dev/null 2>&1; }
port80_listener() { /usr/sbin/lsof -nP -iTCP:80 -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $1" pid"$2; found=1} END{exit !found}'; }

inventory() {
  echo "== Locove 旧 80 系统件盘点 =="
  if entry_plist_present; then
    echo "  [ ours ] ${ENTRY_PLIST}（root 属主 LaunchDaemon）"
    ls -la "$ENTRY_PLIST" 2>/dev/null | sed 's/^/         /'
  else
    echo "  [ absent ] $ENTRY_PLIST 未安装"
  fi
  if entry_loaded; then echo "  [ loaded] system/$ENTRY_LABEL 正在运行"; else echo "  [ idle  ] system/$ENTRY_LABEL 未运行"; fi
  if [ -d "$LIBEXEC_DIR" ]; then
    echo "  [ ours ] ${LIBEXEC_DIR}（root 属主脚本副本）"
    ls -la "$LIBEXEC_DIR" 2>/dev/null | sed 's/^/         /'
  else
    echo "  [ absent ] $LIBEXEC_DIR 不存在"
  fi
  if [ -f "$RESOLVER" ]; then
    echo "  [ note  ] $RESOLVER 存在（归属见 managed-by 标记；默认保留——5670 模式仍需解析）"
  else
    echo "  [ absent ] $RESOLVER 不存在"
  fi
  local l80
  l80="$(port80_listener || true)"
  echo "  80 端口监听：${l80:-（无；非 root 的 lsof 看不到 root 属主监听，以 launchd 状态为准）}"
  echo
  echo "== 迁移计划 =="
  echo "  保留：业务进程/数据/登记、用户级 DNS/Caddy agents（已由 5670 启用流程接管）、"
  echo "        resolver（默认；--remove-resolver 可一并移除）"
  echo "  移除：${ENTRY_PLIST}、${LIBEXEC_DIR}、/Library/Logs/Locove（均为本工具专属命名）"
  echo "  提示：旧无端口书签（http://xxx.locove/）将不可用，请改用 http://xxx.locove:5670/ 。"
}

apply_retire() {
  [ "$(id -u)" = 0 ] || { echo "ERROR: --apply 需要管理员授权（sudo）。dry-run 请直接运行本脚本。" >&2; exit 1; }
  echo "==> 退役 80 转发器"
  if entry_loaded || entry_plist_present; then
    launchctl bootout "system/$ENTRY_LABEL" 2>/dev/null || true
    rm -f "$ENTRY_PLIST"
    echo "  removed: $ENTRY_PLIST"
  else
    echo "  （80 entry 未安装，无需处理）"
  fi
  if [ -d "$LIBEXEC_DIR" ]; then rm -rf "$LIBEXEC_DIR"; echo "  removed: $LIBEXEC_DIR"; fi
  rmdir "/Library/Logs/Locove" 2>/dev/null || true
  if [[ " $* " == *" --remove-resolver "* ]]; then
    echo "==> 移除 resolver（归属守卫生效）"
    (cd "$REPO" && python3 -m localservices.locove_resolver uninstall \
       --dir /etc/resolver --suffix "${LOCOVE_DOMAIN_SUFFIX:-locove}") || true
  else
    echo "==> resolver 保留（默认；5670 域名访问仍需要 .locove 解析）"
  fi
  dscacheutil -flushcache 2>/dev/null || true
  killall -HUP mDNSResponder 2>/dev/null || true
  echo "==> 完成。80 不再由 Locove 占用；$(port80_listener && echo '注意：80 仍有其他进程监听（非本工具）' || echo '80 已空闲。')"
}

case " $* " in
  *" --apply "*) apply_retire "$@" ;;
  *) inventory ;;
esac
