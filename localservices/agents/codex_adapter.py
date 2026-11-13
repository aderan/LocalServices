"""codex CLI 适配器：一次有界 `codex exec` 调用（只读沙箱 + 结构化输出），非常驻代理。"""

from __future__ import annotations

import json
from pathlib import Path

from .base import extract_json, read_text_safe


def _require_all_properties(schema: dict) -> None:
    """Make omitted fields representable as null in Codex structured output."""
    properties = schema.get("properties")
    if properties is not None:
        required = set(schema.get("required", ()))
        for name, child in properties.items():
            if name not in required:
                types = child["type"]
                if isinstance(types, str):
                    types = [types]
                if "null" not in types:
                    child["type"] = [*types, "null"]
            _require_all_properties(child)
        schema["required"] = list(properties)
    if "items" in schema:
        _require_all_properties(schema["items"])


class CodexAdapter:
    kind = "codex"

    def __init__(self, bin_path: str):
        self.bin = bin_path

    def build_argv(self, directory: str, prompt_file: str, schema_path: str,
                   timeout: float, last_message_path: str | None = None) -> list[str]:
        """构造 argv。codex 无超时旗标，timeout 由调用方的 subprocess 超时强制执行。"""
        prompt = read_text_safe(prompt_file, limit=200_000)
        schema = json.loads(Path(schema_path).read_text(encoding="utf-8"))
        _require_all_properties(schema)
        strict_schema = Path(prompt_file).with_name("codex-output-schema.json")
        strict_schema.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
        argv = [
            self.bin, "exec",
            "--skip-git-repo-check",   # 临时空目录不是 git 仓库
            "--ephemeral",             # 不落 session 文件
            "-s", "read-only",         # 只读沙箱：模型不能改文件/跑写操作
            "-C", directory,           # 目标目录以参数传入，绝不作为 cwd
            "--output-schema", str(strict_schema),
        ]
        if last_message_path:
            argv += ["-o", last_message_path]
        argv.append(prompt if prompt else "-")
        return argv

    def parse_output(self, raw: str | bytes, last_message_path: str | None = None) -> dict:
        """优先读 -o 落盘的最终消息（结构化 JSON），退回 stdout。"""
        texts: list[str] = []
        if last_message_path:
            t = read_text_safe(last_message_path)
            if t:
                texts.append(t)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        texts.append(raw)
        last_err: Exception | None = None
        for t in texts:
            try:
                data = extract_json(t)
            except ValueError as e:
                last_err = e
                continue

            # The shared schema represents absent optional arrays by omitting the key.
            services = data.get("services") if isinstance(data, dict) else None
            if isinstance(services, list):
                for service in services:
                    components = service.get("components") if isinstance(service, dict) else None
                    if not isinstance(components, list):
                        continue
                    for component in components:
                        if isinstance(component, dict):
                            for field in ("cmd", "compose_files"):
                                if field in component and component[field] is None:
                                    del component[field]
            return data
        raise ValueError(f"codex 输出无 JSON: {last_err}")
