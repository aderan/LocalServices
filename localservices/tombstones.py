"""用户配置层移除标记（tombstone，CONTRACTS V2.0）。

removed.json 记录用户主动移除的服务/项目；manifest.load_all 据此跳过对应内置模板，
使"移除内置服务"不修改随仓分发的 services/*.toml。同 id 重新注册解除标记。
本文件只读；写入端在 registry.py（Core/Lifecycle 线所有权）。
"""

from __future__ import annotations

import json

from . import paths


def _load() -> dict:
    try:
        data = json.loads(paths.removed_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def services() -> set[str]:
    """被移除服务的 id 集合（坏文件按空处理，绝不覆盖原文件）。"""
    services = _load().get("services")
    if isinstance(services, dict):
        return set(services.keys())
    return set()


def projects() -> set[str]:
    removed = _load().get("projects")
    if isinstance(removed, dict):
        return set(removed.keys())
    return set()
