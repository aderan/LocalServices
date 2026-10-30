#!/bin/bash
# Install / update the LocalServices Codex skill (CONTRACTS §6; idempotent).
#   source: <repo>/skills/localservices  ->  target: ~/.codex/skills/localservices
# 冲突策略：目标目录已存在时——
#   * 安装记录表明是我们装的，且每个已装文件 sha256 与记录一致 -> 原地更新；
#   * 否则（第三方目录 / 用户改过 / 记录损坏）-> 整目录备份为
#     <target>.user-bak-<ts>（不丢任何用户内容），再安装新版并打印备份路径。
# 卸载（--uninstall 或 scripts/uninstall-skill.sh）：只删除记录在案且 sha256 仍匹配的
# 文件；用户改过或新增的保留；目录清空后连目录一起删；最后更新安装记录。
# 环境覆盖（测试/将来扩展到其他宿主）：
#   LOCALSERVICES_SKILL_DIR  目标目录（默认 ~/.codex/skills/localservices）
#   LOCALSERVICES_SKILL_SRC  源目录（默认 <repo>/skills/localservices；测试用）
#   LOCALSERVICES_HOME       安装记录所在（默认 ~/Library/Application Support/LocalServices）
# 记录文件：$LOCALSERVICES_HOME/skill-install.json（每文件 sha256、installed_at、source_commit）。
# 仅依赖 bash + shasum/sha256sum（自动检测）。

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${LOCALSERVICES_SKILL_SRC:-$REPO/skills/localservices}"
SKILL_DIR="${LOCALSERVICES_SKILL_DIR:-$HOME/.codex/skills/localservices}"
LS_HOME="${LOCALSERVICES_HOME:-$HOME/Library/Application Support/LocalServices}"
RECORD="$LS_HOME/skill-install.json"

# ---------- 基础工具 ----------

if command -v shasum >/dev/null 2>&1; then
  sha256_of() { shasum -a 256 "$1" | awk '{print $1}'; }
elif command -v sha256sum >/dev/null 2>&1; then
  sha256_of() { sha256sum "$1" | awk '{print $1}'; }
else
  echo "错误：需要 shasum（macOS）或 sha256sum 才能校验文件。" >&2
  exit 1
fi

json_quote() {  # 仅处理路径中现实的字符（引号/反斜杠），足够安全
  local s=${1//\\/\\\\}
  s=${s//\"/\\\"}
  printf '"%s"' "$s"
}

# 记录文件读取（我们自己是唯一写方，格式固定；解析不出即视为损坏 -> 按第三方处理）
record_target() { sed -n 's/^  "target": "\(.*\)",$/\1/p' "$RECORD" 2>/dev/null | head -n 1; }
record_entries() {  # 每行 "relpath sha256"
  sed -n 's/^    "\([^"]*\)": "\([0-9a-f]\{64\}\)",\{0,1\}$/\1 \2/p' "$RECORD" 2>/dev/null
}

write_record() {  # $1 = manifest 文件（每行 "relpath sha256"）
  local mf=$1 now commit first=1
  now="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  commit="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || echo unknown)"
  local body
  body="$(mktemp "${TMPDIR:-/tmp}/ls-skill-record.XXXXXX")"
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    if [ "$first" -eq 1 ]; then first=0; else printf ',\n' >>"$body"; fi
    printf '    %s: "%s"' "$(json_quote "${line%% *}")" "${line##* }" >>"$body"
  done <"$mf"
  mkdir -p "$LS_HOME"
  {
    echo '{'
    echo "  \"version\": 1,"
    echo "  \"target\": $(json_quote "$SKILL_DIR"),"
    echo "  \"installed_at\": \"$now\","
    echo "  \"source_commit\": \"$commit\","
    echo '  "files": {'
    cat "$body"
    echo ''
    echo '  }'
    echo '}'
  } >"$RECORD.tmp.$$"
  mv "$RECORD.tmp.$$" "$RECORD"
  rm -f "$body"
}

source_files() {  # 源目录内相对路径（排序，排除 .DS_Store）
  (cd "$SRC" && find . -type f ! -name '.DS_Store' | sed 's|^\./||' | LC_ALL=C sort)
}

die() { echo "错误：$*" >&2; exit 1; }

# ---------- 卸载 ----------

uninstall_skill() {
  [ -f "$RECORD" ] || die "没有安装记录（${RECORD}），不是本工具安装的，未删除任何文件。"
  local rec_target
  rec_target="$(record_target)"
  [ -n "$rec_target" ] || die "安装记录损坏（读不出 target），按保守策略不删除任何文件。"
  [ "$rec_target" = "$SKILL_DIR" ] \
    || die "记录的目标目录是 ${rec_target}，与当前 $SKILL_DIR 不一致；请用 LOCALSERVICES_SKILL_DIR 指向记录值。"
  [ -d "$SKILL_DIR" ] || { echo "目标目录不存在，无事可做。"; exit 0; }

  local removed=0 kept manifest
  manifest="$(mktemp "${TMPDIR:-/tmp}/ls-skill-keep.XXXXXX")"
  : >"$manifest"
  while read -r rel hash; do
    [ -n "$rel" ] || continue
    local f="$SKILL_DIR/$rel"
    if [ -f "$f" ] && [ "$(sha256_of "$f")" = "$hash" ]; then
      rm -f "$f"
      removed=$((removed + 1))
    else
      echo "$rel $hash" >>"$manifest"   # 用户改过/已缺失：保留并继续记账
      echo "保留（与记录不符或用户改动）：$rel"
    fi
  done < <(record_entries)

  # 清掉空目录（rmdir 只删空目录，非空报错即跳过）
  find "$SKILL_DIR" -depth -type d -print0 | xargs -0 rmdir >/dev/null 2>&1 || true

  if [ -s "$manifest" ]; then
    write_record "$manifest"
    echo "已卸载 $removed 个文件；目录因存在保留文件而未删除：${SKILL_DIR}（见上方列表）。"
  else
    rm -f "$RECORD"
    if [ -d "$SKILL_DIR" ]; then
      echo "已卸载 $removed 个文件；目录因含非本工具文件未删除：${SKILL_DIR}。"
    else
      echo "已卸载 $removed 个文件，目录已删除：$SKILL_DIR"
    fi
  fi
  rm -f "$manifest"
  exit 0
}

[ "${1:-}" = "--uninstall" ] && uninstall_skill
[ $# -gt 0 ] && die "未知参数：$1（仅支持 --uninstall）"

# ---------- 安装 ----------

[ -f "$SRC/SKILL.md" ] || die "源目录缺少 SKILL.md：$SRC"

FILES="$(source_files)"
[ -n "$FILES" ] || die "源目录为空：$SRC"
while IFS= read -r rel; do
  case "$rel" in ''|*[[:space:]]*) die "源文件名含空白，安装器不支持：$rel";; esac
done <<<"$FILES"

if [ -d "$SKILL_DIR" ]; then
  [ "$(cd "$SRC" && pwd)" = "$(cd "$SKILL_DIR" && pwd)" ] && die "源与目标是同一目录。"
fi

# 判定模式：fresh / update（我们装的且未改动）/ takeover（备份后安装）
MODE=fresh
BACKUP=""
if [ -d "$SKILL_DIR" ]; then
  MODE=takeover
  if [ -f "$RECORD" ] && [ "$(record_target)" = "$SKILL_DIR" ]; then
    MODE=update
    while read -r rel hash; do
      [ -n "$rel" ] || continue
      f="$SKILL_DIR/$rel"
      if [ ! -f "$f" ] || [ "$(sha256_of "$f")" != "$hash" ]; then
        MODE=takeover   # 记录在案的文件被改动/删除 -> 视为用户内容
        break
      fi
    done < <(record_entries)
  fi
fi

if [ "$MODE" = "takeover" ] && [ -d "$SKILL_DIR" ]; then
  ts="$(date +%Y%m%d-%H%M%S)"
  BACKUP="$(dirname "$SKILL_DIR")/$(basename "$SKILL_DIR").user-bak-$ts"
  n=0
  while [ -e "$BACKUP" ]; do n=$((n + 1)); BACKUP="$(dirname "$SKILL_DIR")/$(basename "$SKILL_DIR").user-bak-$ts-$n"; done
  mkdir -p "$(dirname "$BACKUP")"
  cp -R "$SKILL_DIR" "$BACKUP"
fi

# 复制新版（update 模式顺带清掉新版已移除、且未被改动的旧文件）
if [ "$MODE" = "update" ]; then
  while read -r rel hash; do
    [ -n "$rel" ] || continue
    case "$FILES" in *"$rel"*) continue;; esac
    f="$SKILL_DIR/$rel"
    if [ -f "$f" ] && [ "$(sha256_of "$f")" = "$hash" ]; then rm -f "$f"; fi
  done < <(record_entries)
fi
mkdir -p "$SKILL_DIR"
while IFS= read -r rel; do
  mkdir -p "$SKILL_DIR/$(dirname "$rel")"
  cp -p "$SRC/$rel" "$SKILL_DIR/$rel"
done <<<"$FILES"

# 记账：对装好的目标文件逐个取 sha256
MANIFEST="$(mktemp "${TMPDIR:-/tmp}/ls-skill-manifest.XXXXXX")"
: >"$MANIFEST"
while IFS= read -r rel; do
  echo "$rel $(sha256_of "$SKILL_DIR/$rel")" >>"$MANIFEST"
done <<<"$FILES"
write_record "$MANIFEST"
rm -f "$MANIFEST"

case "$MODE" in
  fresh)   echo "已安装 LocalServices skill：${SKILL_DIR}（$(printf '%s\n' "$FILES" | wc -l | tr -d ' ') 个文件）";;
  update)  echo "已更新 LocalServices skill：${SKILL_DIR}（$(printf '%s\n' "$FILES" | wc -l | tr -d ' ') 个文件）";;
  takeover) echo "已安装 LocalServices skill：${SKILL_DIR}（$(printf '%s\n' "$FILES" | wc -l | tr -d ' ') 个文件）"
            echo "目标目录原有内容（非本工具安装或已被修改）已完整备份到：$BACKUP";;
esac
echo "安装记录：$RECORD"
