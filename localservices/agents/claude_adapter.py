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
        argv = [
            self.bin, "-p",
            "--output-format", "json",
            "--tools", "Read,Glob,Grep",  # 只读工具白名单：无 Bash/Edit/Write
            "--permission-prompts", "none",
            "--add-dir", directory,
        ]
        if schema_text:
            argv += ["--json-schema", schema_text]
        argv.append(prompt if prompt else "Reply with a JSON object.")
        return argv

    def parse_output(self, raw: str | bytes, last_message_path: str | None = None) -> dict:
        """stdout 是 JSON envelope {"type":"result","result":...}；result 里可能是围栏 JSON。"""
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        try:
            env = json.loads(raw)
        except ValueError as e:
            raise ValueError(f"claude stdout 不是 JSON: {e}") from e
        if not isinstance(env, dict) or "result" not in env:
            raise ValueError(f"claude envelope 缺 result: {str(env)[:200]}")
        if env.get("is_error"):
            raise ValueError(f"claude 返回错误: {str(env.get('result', ''))[:200]}")
        return extract_json(str(env["result"]))
