"""只读目录证据扫描（契约 §3）：把 Makefile / package.json / pyproject / compose /
README / .env.example 提炼成给模型 prompt 用的资料。绝不执行目录里的任何东西。"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

IGNORED_DIRS = {"node_modules", ".git", "dist", "build", ".venv", "venv",
                "__pycache__", "coverage", ".next", "target"}
MAX_FILE_BYTES = 1_000_000  # >1MB 不读内容
MAX_DETAIL = 400

# Makefile 里值得单独摘出的“运行入口”行（行级：端口/绑定/服务特征，避免 dev/run 子串误匹配 DEVICE 等）
MAKE_INTERESTING = ("sources-manager", "--port", "127.0.0.1", "0.0.0.0", "localhost", "--bind", "serve")
# 运行入口类目标名：目标列表超限时优先保留（如 sources-manager、api-dev）
RUN_TARGET = re.compile(r"(manager|admin|serve|api|web|ui|dev|run|start|dash)", re.IGNORECASE)
# README 代码块里认可的命令前缀
README_CMD = re.compile(r"^\s*(make|npm|npx|yarn|pnpm|python3?|uv|pip3?)\b")
COMPOSE_NAMES = re.compile(r"^(docker-)?compose", re.IGNORECASE)


def _clip(s: str, limit: int = MAX_DETAIL) -> str:
    s = " ".join(s.split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _makefile_detail(text: str) -> str:
    targets: list[str] = []
    interesting: list[str] = []
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        body = line.split("#")[0].rstrip()
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.%-]*)\s*:", body)
        if m and m.group(1) not in (".PHONY", ".DEFAULT", ".SUFFIXES") and m.group(1) not in targets:
            targets.append(m.group(1))
        low = line.lower()
        if any(k in low for k in MAKE_INTERESTING) and line.strip() and not line.startswith("#") \
                and not body.lstrip().startswith(".PHONY"):
            # recipe 行（TAB 开头）携带真实命令与端口，同样值得摘出
            candidate = line.strip()
            if len(candidate) > 140:
                candidate = candidate[:139] + "…"
            if candidate not in interesting:
                interesting.append(candidate)
    # 真正带端口的行优先（如 --port 8797），纯关键词行其后
    portish = re.compile(r"port\D*\d{2,5}|:\d{4,5}", re.I)
    interesting = [s for _, _, s in sorted(
        ((0 if portish.search(s) else 1, i, s) for i, s in enumerate(interesting)))]
    # 目标太多时先保运行入口类，再保其余（真实服务常在大型 Makefile 深处）
    run = [t for t in targets if RUN_TARGET.search(t)]
    others = [t for t in targets if not RUN_TARGET.search(t)]
    ordered = run + others
    parts = []
    if ordered:
        parts.append(_clip("targets: " + ", ".join(ordered[:24]), 210))
    if interesting:
        parts.append(_clip("lines: " + " | ".join(interesting[:8]), 170))
    return " ".join(s for s in parts if s)


def _package_detail(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    parts = []
    if isinstance(data.get("name"), str):
        parts.append(f"name: {data['name']}")
    scripts = data.get("scripts")
    if isinstance(scripts, dict):
        parts.append("scripts: " + ", ".join(str(k) for k in list(scripts)[:20]))
    return _clip("; ".join(parts))


def _pyproject_detail(text: str) -> str:
    scripts: list[str] = []
    tools: list[str] = []
    section = ""
    for line in text.splitlines():
        s = line.strip()
        m = re.match(r"^\[([^\]]+)\]$", s)
        if m:
            section = m.group(1)
            if section.startswith("tool."):
                tools.append(section)
            continue
        if section in ("project.scripts", "scripts") and "=" in s:
            name = s.split("=", 1)[0].strip()
            if name:
                scripts.append(name)
    parts = []
    if scripts:
        parts.append("scripts: " + ", ".join(scripts[:20]))
    if tools:
        parts.append("tools: " + ", ".join(tools[:10]))
    return _clip("; ".join(parts))


def _compose_services(text: str) -> list[str]:
    """容忍式解析：只认顶层 `services:` 下两空格缩进键；结构怪异就静默放弃。"""
    services: list[str] = []
    in_services = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            in_services = line.strip() == "services:"
            continue
        if in_services and indent == 2:
            key = line.strip().split(":", 1)[0].strip()
            if key and not key.startswith("-") and not key.startswith("#"):
                services.append(key)
    return services[:20]


def _readme_detail(text: str) -> str:
    headings: list[str] = []
    commands: list[str] = []
    in_block = False
    for line in text.splitlines()[:60]:
        if line.strip().startswith("```"):
            in_block = not in_block
            continue
        m = re.match(r"^#{1,4}\s+(.+)$", line)
        if m and m.group(1).strip():
            headings.append(m.group(1).strip())
        elif in_block and README_CMD.match(line):
            if line.strip() not in commands:
                commands.append(line.strip())
    parts = []
    if headings:
        parts.append("headings: " + " / ".join(headings[:8]))
    if commands:
        parts.append("commands: " + " | ".join(commands[:10]))
    return _clip("; ".join(parts))


def _env_detail(text: str) -> str:
    keys: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        keys.append(s.split("=", 1)[0].strip())
    return _clip("keys: " + ", ".join(keys[:30]) + "（值已脱敏）")


def _one_entry(rel: Path, kind: str, detail: str) -> dict | None:
    if not detail:
        return None
    return {"path": str(rel), "kind": kind, "detail": detail}


def _read_small(path: Path) -> str:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _handle_file(root: Path, rel: Path, path: Path) -> dict | None:
    name = path.name
    if name == ".env.example":
        return _one_entry(rel, "env_example", _env_detail(_read_small(path)))
    if name.startswith("."):
        return None
    lower = name.lower()
    if name in ("Makefile", "makefile", "GNUmakefile"):
        return _one_entry(rel, "makefile", _makefile_detail(_read_small(path)))
    if name == "package.json":
        return _one_entry(rel, "package_json", _package_detail(path))
    if name == "pyproject.toml":
        return _one_entry(rel, "pyproject", _pyproject_detail(_read_small(path)))
    if lower.startswith("readme") and lower.endswith((".md", ".markdown")):
        return _one_entry(rel, "readme", _readme_detail(_read_small(path)))
    if COMPOSE_NAMES.match(name) and name.endswith((".yml", ".yaml")):
        svc = _compose_services(_read_small(path))
        return _one_entry(rel, "compose", _clip("services: " + ", ".join(svc)) if svc else "")
    return None


def scan_directory(directory: str | Path, max_depth: int = 3, max_files: int = 5000,
                   time_budget: float = 8.0) -> dict:
    """收集给模型用的入口证据。返回 {"directory", "entries": [...], "truncated": bool}。"""
    root = Path(directory).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"not a directory: {directory}")
    entries: list[dict] = []
    truncated = False
    deadline = time.monotonic() + time_budget
    files_seen = 0

    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        depth = len(rel_dir.parts)  # 本层文件相对路径 = depth + 1 个组成部分
        in_limit = depth + 1 <= max_depth
        # 安装线索：常见依赖目录的存在性（仅作为证据，不进入其中；需在剪枝前看原始列表）
        if in_limit:
            for hint in ("node_modules", ".venv", "venv"):
                if hint in dirnames:
                    kind = "node_modules" if hint == "node_modules" else "venv"
                    entries.append({"path": str(rel_dir / hint) if str(rel_dir) != "." else hint,
                                    "kind": "install_hint",
                                    "detail": _clip(f"{kind} 存在，依赖已安装")})
        # 隐藏目录与忽略目录整枝剪掉；深度用尽不再下探
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in IGNORED_DIRS and not d.startswith(".")
        )
        if not in_limit or depth + 1 == max_depth:
            dirnames[:] = []
        if not in_limit:
            continue
        for name in sorted(filenames):
            files_seen += 1
            if files_seen > max_files or time.monotonic() > deadline:
                truncated = True
                break
            rel = rel_dir / name if str(rel_dir) != "." else Path(name)
            entry = _handle_file(root, rel, Path(dirpath) / name)
            if entry:
                entries.append(entry)
        if truncated:
            break
    return {"directory": str(root), "entries": entries, "truncated": truncated}
