"""AgentConnection 模型与持久化（agent-connections.json，契约 §1/§4）。

文件结构：{"schemaVersion": 1, "default": "codex", "connections": {kind: {...}}}。
状态只能由 detect.scan/detect.verify 产生；本模块只管形状与读写。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .. import paths

KINDS = ("codex", "claude")
SCHEMA_VERSION = 1

# 只有 verify（真实最小调用）能产出这三个状态
VERIFIED_STATUSES = ("needs_login", "call_failed", "available")
ALL_STATUSES = ("not_installed", "config_only", "not_executable") + VERIFIED_STATUSES


@dataclass
class AgentConnection:
    kind: str
    path: str
    version: str
    config_root: str
    status: str  # not_installed|config_only|not_executable|needs_login|call_failed|available
    detail: str = ""
    verified_at: float = 0.0
    is_default: bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AgentConnection":
        """宽容读取：未知字段忽略、缺字段用默认值（旧缓存文件升级不炸）。"""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    def replace(self, **kw) -> "AgentConnection":
        d = self.to_dict()
        d.update(kw)
        return AgentConnection.from_dict(d)


def load_connections() -> tuple[dict[str, AgentConnection], str]:
    """读取缓存，返回 (connections, default)。坏文件按无缓存处理。"""
    try:
        data = json.loads(paths.agent_connections_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, "codex"
    if not isinstance(data, dict):
        return {}, "codex"
    conns: dict[str, AgentConnection] = {}
    raw = data.get("connections")
    if isinstance(raw, dict):
        for kind, d in raw.items():
            if kind in KINDS and isinstance(d, dict):
                try:
                    conns[kind] = AgentConnection.from_dict(d)
                except TypeError:
                    continue
    default = data.get("default")
    if default not in conns and default not in KINDS:
        default = "codex" if "codex" in conns else (next(iter(conns)) if conns else "codex")
    return conns, default


def save_connections(conns: dict[str, AgentConnection], default: str | None = None) -> str:
    """原子写入（临时文件 + os.replace）；default 无效时回落 codex。返回生效的 default。"""
    if default not in conns:
        default = "codex" if "codex" in conns else (next(iter(conns)) if conns else "codex")
    payload = {"schemaVersion": SCHEMA_VERSION, "default": default, "connections": {}}
    for kind, c in conns.items():
        c.is_default = kind == default
        payload["connections"][kind] = c.to_dict()
    p = paths.agent_connections_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)
    return default


def extract_json(text: str) -> dict:
    """从模型输出提取 JSON：剥离 ```json 围栏；失败再按围栏正则兜底。"""
    t = (text or "").strip()
    if not t:
        raise ValueError("empty output")
    if t.startswith("```"):
        lines = t.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    try:
        return json.loads(t)
    except ValueError:
        pass
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if m:
        return json.loads(m.group(1))
    raise ValueError("no JSON object in output")


def read_text_safe(path: str | Path, limit: int = 1_000_000) -> str:
    """读文本，>limit 字节跳过内容（返回空串），任何 OSError 都吞掉。"""
    try:
        p = Path(path)
        if p.stat().st_size > limit:
            return ""
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
