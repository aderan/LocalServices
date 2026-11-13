"""claude CLI 适配器：一次有界 `claude -p` 调用（工具白名单只读），非常驻代理。

只读方案（依据 `claude --help`，2.1.308 实测旗标）：
- `--tools Read,Glob,Grep`：只保留读类内置工具——无 Bash/Edit/Write，物理上不可执行
  命令或改文件（契约 §3 "claude 只允许 Read/Glob/Grep 类工具"）；
- `--permission-prompts none`：非交互下一切需要审批的动作自动拒绝；
- 目标目录经 `--add-dir` 授予访问，进程 cwd 始终是临时空目录。
注：不用 `--restricted`——它会忽略用户 settings，本机经 settings 配置的模型网关
认证会被一并丢掉（实测返回 "Not logged in"）。
"""

from __future__ import annotations

import json
from pathlib import Path

from .base import extract_json, read_text_safe


class ClaudeAdapter:
    kind = "claude"

    def __init__(self, bin_path: str):
        self.bin = bin_path

    def build_argv(self, directory: str, prompt_file: str, schema_path: str,
                   timeout: float, last_message_path: str | None = None) -> list[str]:
        """构造 argv。claude 无超时旗标，timeout 由调用方的 subprocess 超时强制执行。
        last_message_path 仅为接口对齐保留（claude 直接用 stdout 的 JSON envelope）。"""
        prompt = read_text_safe(prompt_file, limit=200_000)
        schema_text = read_text_safe(schema_path, limit=200_000)
        if schema_text:
            # claude 的校验器只认 draft-07 方言；剥掉 2020-12 的 $schema 声明，
            # 其余关键字（type/required/enum/pattern/min/max/items）两方言语义一致。
            try:
                schema = json.loads(schema_text)
                schema.pop("$schema", None)
                schema_text = json.dumps(schema, ensure_ascii=False)
            except ValueError:
                pass
        argv = [
            self.bin, "-p",
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--tools", "Read,Glob,Grep",  # 只读工具白名单：无 Bash/Edit/Write
            "--permission-prompts", "none",
            "--add-dir", directory,
        ]
        if schema_text:
            argv += ["--json-schema", schema_text]
        argv.append(prompt if prompt else "Reply with a JSON object.")
        return argv

    @staticmethod
    def activity(event: dict):
        """Project CLI events into fixed labels without forwarding message contents."""
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            yield "cli_ready"
        elif kind == "stream_event" and (event.get("event") or {}).get("type") == "message_start":
            yield "model_response"
        elif kind == "assistant":
            tools = {"Read": "tool_read", "Glob": "tool_glob", "Grep": "tool_grep"}
            for block in (event.get("message") or {}).get("content", []):
                if block.get("type") == "tool_use" and block.get("name") in tools:
                    yield tools[block["name"]]
        elif kind == "result":
            yield "cli_result"

    @staticmethod
    def display_output(event: dict):
        if event.get("type") == "assistant":
            for block in (event.get("message") or {}).get("content", []):
                if block.get("type") == "text":
                    yield block.get("text", "")
                elif block.get("type") == "tool_use":
                    tool = block.get("name")
                    target_key = {"Read": "file_path", "Glob": "pattern", "Grep": "pattern"}.get(tool)
                    if target_key:
                        args = block.get("input") or {}
                        target = args.get(target_key) or ""
                        location = args.get("path")
                        yield f"{tool}: {target}" + (f" ({location})" if location else "")
        elif event.get("type") == "result" and event.get("is_error"):
            yield str(event.get("result") or event.get("errors") or event.get("subtype", ""))

    def parse_output(self, raw: str | bytes, last_message_path: str | None = None) -> dict:
        """Parse the final stream event or a legacy JSON result envelope."""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            env = json.loads(raw)
        except ValueError as e:
            raise ValueError(f"claude stdout 不是 JSON: {e}") from e
        if not isinstance(env, dict):
            raise ValueError("Claude 结果不是 JSON 对象")
        if env.get("is_error"):
            reason = env.get("result") or env.get("errors") or env.get("subtype", "")
            raise ValueError(f"Claude 返回错误: {str(reason)[:200]}")
        if isinstance(env.get("structured_output"), dict):
            return env["structured_output"]
        return extract_json(str(env.get("result", "")))
