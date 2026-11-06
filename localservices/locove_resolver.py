"""Split-DNS resolver 文件的所有权保护（review R3）。

`/etc/resolver/<suffix>` 只在「不存在」或「内容带本工具管理标记」时才允许写入/删除。
他人配置（用户手写、其他工具生成）一律拒绝并保持原样——安装绝不覆盖，卸载绝不删除，
报错里给出人工核对指引。安装/卸载脚本经 `python3 -m localservices.locove_resolver`
调用本模块；逻辑在此单测，shell 只做包装。"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

MARKER = "# managed-by: locove (com.mybrain.ls)"
# 2026-11-04 之前的安装器写的头部（无 managed-by 标记）——那也是本工具的文件，
# 升级/卸载必须继续认领，不能被自己的所有权检查挡住
LEGACY_HEADER = "# Locove／洛湾 split DNS"


def owned(content: str) -> bool:
    if MARKER in content:
        return True
    first = next((ln for ln in content.splitlines() if ln.strip()), "")
    return first.startswith(LEGACY_HEADER)


def resolver_path(resolver_dir: str | Path, suffix: str) -> Path:
    return Path(resolver_dir) / suffix


def _body(suffix: str, dns_port: int) -> str:
    return (f"{MARKER}\n"
            f"# Locove／洛湾 split DNS：仅把 .{suffix} 交给本地 DNS（由 LocalServices 管理）\n"
            f"nameserver 127.0.0.1\n"
            f"port {dns_port}\n"
            f"search_order 1\n")


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def install(resolver_dir: str | Path, dns_port: int, suffix: str = "locove") -> dict:
    path = resolver_path(resolver_dir, suffix)
    if path.exists():
        content = _read(path)
        if content is None:
            return {"ok": False, "path": str(path), "reason": "unreadable",
                    "error": f"无法读取 {path}（权限？），拒绝覆盖。"}
        if not owned(content):
            return {"ok": False, "path": str(path), "reason": "foreign",
                    "error": f"{path} 已存在且不属于 Locove（缺少管理标记 {MARKER!r}），"
                    "拒绝覆盖。请先人工核对该文件的用途：确属废弃再手动移除，"
                    f"或将其内容合并后删除标记行以外部分重试。当前内容开头：{content[:200]!r}"}
    path.parent.mkdir(parents=True, exist_ok=True)
    # 原子替换：临时文件 + rename，绝不让半截文件留在 /etc/resolver
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{suffix}.new-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_body(suffix, dns_port))
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return {"ok": True, "path": str(path), "action": "installed"}


def uninstall(resolver_dir: str | Path, suffix: str = "locove") -> dict:
    path = resolver_path(resolver_dir, suffix)
    if not path.exists():
        return {"ok": True, "path": str(path), "action": "absent"}
    content = _read(path)
    if content is None:
        # S2（review 2026-11-05）：读失败 ≠ 已确认归属——I/O/权限失败时绝不删除
        return {"ok": False, "path": str(path), "reason": "unreadable",
                "error": f"无法读取 {path}（权限或 I/O 失败），无法确认归属，拒绝删除；文件保持原样。"}
    if not owned(content):
        return {"ok": False, "path": str(path), "reason": "foreign",
                "error": f"{path} 内容不属于 Locove（缺少管理标记），拒绝删除；文件保持原样。"}
    try:
        path.unlink()
    except OSError as e:
        return {"ok": False, "path": str(path), "reason": "unlink-failed", "error": str(e)}
    return {"ok": True, "path": str(path), "action": "removed"}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="locove-resolver")
    ap.add_argument("action", choices=["install", "uninstall"])
    ap.add_argument("--dir", required=True, help="resolver 目录（生产为 /etc/resolver）")
    ap.add_argument("--port", type=int, default=5354, help="本地 DNS 端口（install 用）")
    ap.add_argument("--suffix", default=os.environ.get("LOCALSERVICES_DOMAIN_SUFFIX", "locove"))
    args = ap.parse_args(argv)
    res = (install(args.dir, args.port, args.suffix) if args.action == "install"
           else uninstall(args.dir, args.suffix))
    if not res.get("ok"):
        print(f"ERROR: {res.get('error', res)}", file=sys.stderr)
        return 1
    print(f"{args.action.upper()} OK: {res['path']} ({res.get('action')})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
