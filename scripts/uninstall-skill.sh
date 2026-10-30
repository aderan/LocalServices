#!/bin/bash
# Uninstall the LocalServices Codex skill: delegate to install-skill.sh --uninstall.
# 只删除安装记录在案且 sha256 仍匹配的文件；用户改过/新增的内容保留。
set -euo pipefail
exec "$(cd "$(dirname "$0")" && pwd)/install-skill.sh" --uninstall "$@"
