#!/bin/bash
# 把 *.locove 追加到所有网络服务的系统代理绕行表（加法式：保留既有条目；幂等；可反复执行）。
# 需要管理员权限：由管理员授权对话框/sudo 调用。
# 背景：本机系统代理(127.0.0.1:7897)会拦截 .locove 并按公网域名上游解析 → 502；
# 加入绕行表后浏览器对 *.locove 直连本地入口（与 127.0.0.1 等私有网段既有绕行同法）。
# 回退：networksetup -setproxybypassdomains <service> <原条目...>（脚本打印修改前快照）。
set -euo pipefail
SUFFIX="${1:-locove}"
while IFS= read -r raw; do
  svc="${raw#\*}"           # 剥离禁用服务的前导 *
  [ -z "$svc" ] && continue
  # 读取失败 = 该服务无代理配置（rc=8 "proxies = nil"）：没有绕行表需要改
  if ! cur="$(networksetup -getproxybypassdomains "$svc" 2>/dev/null)"; then
    echo "[$svc] 无代理配置，跳过"
    continue
  fi
  # 只认合法的绕行条目行（防错误/提示文本混入）；已包含则跳过（幂等）
  entries="$(printf '%s\n' "$cur" | grep -Ev '^(\*\* Error|Error:|There|aren|any|bypass|domains|set|on\.|$)' || true)"
  case "$cur" in *"aren't any bypass domains"*) entries="";; esac
  if printf '%s\n' "$entries" | grep -Fxq "*.${SUFFIX}"; then
    echo "[$svc] 已包含 *.${SUFFIX}，跳过"
    continue
  fi
  echo "[$svc] 修改前绕行表：$(printf '%s ' $entries)"
  # shellcheck disable=SC2086
  networksetup -setproxybypassdomains "$svc" ${entries} "*.${SUFFIX}" 2>/dev/null     || networksetup -setproxybypassdomains "$svc" "*.${SUFFIX}"
  echo "[$svc] 已追加 *.${SUFFIX}"
done < <(networksetup -listallnetworkservices 2>/dev/null | tail -n +2)
