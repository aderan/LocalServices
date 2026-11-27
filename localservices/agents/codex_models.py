"""Resolve a Codex model with the same binary, configuration, and connection as exec."""

from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import __version__
from . import detect


@dataclass
class Catalog:
    configured: str | None
    models: set[str]
    default: str | None
    config_known: bool


def read_catalog(binary: str, workdir: str, directory: str, env: dict, timeout: float) -> Catalog | None:
    """Ask a short-lived app-server for the effective config and its model catalog."""
    deadline = time.monotonic() + timeout
    try:
        with subprocess.Popen([binary, "app-server", "--listen", "stdio://"], cwd=workdir, env=env,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              start_new_session=True) as proc, selectors.DefaultSelector() as selector:
            buffer = bytearray()
            selector.register(proc.stdout, selectors.EVENT_READ)
            request_id = 0

            def send(message: dict) -> None:
                proc.stdin.write((json.dumps(message) + "\n").encode())
                proc.stdin.flush()

            def request(method: str, params: dict) -> dict | None:
                nonlocal request_id
                request_id += 1
                send({"method": method, "id": request_id, "params": params})
                while True:
                    while (end := buffer.find(b"\n")) >= 0:
                        line = bytes(buffer[:end])
                        del buffer[:end + 1]
                        try:
                            reply = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(reply, dict) and reply.get("id") == request_id:
                            result = reply.get("result")
                            return result if isinstance(result, dict) else None
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        raise TimeoutError
                    chunk = os.read(proc.stdout.fileno(), 65536)
                    if not chunk or len(buffer) + len(chunk) > 2_000_000:
                        raise EOFError
                    buffer.extend(chunk)

            try:
                if request("initialize", {"clientInfo": {"name": "locove", "version": __version__}}) is None:
                    return None
                send({"method": "initialized", "params": {}})
                effective = request("config/read", {"cwd": directory, "includeLayers": False}) or {}
                config = effective.get("config")
                configured = config.get("model") if isinstance(config, dict) else None
                models: set[str] = set()
                default = None
                params = {"includeHidden": True}
                if isinstance(config, dict) and isinstance(provider := config.get("model_provider"), str) and provider:
                    params["modelProvider"] = provider
                while True:
                    page = request("model/list", params)
                    if page is None or not isinstance(page.get("data"), list):
                        return None
                    for row in page["data"]:
                        if isinstance(row, dict) and isinstance(model := row.get("model"), str) and model:
                            models.add(model)
                            if row.get("isDefault") is True and not row.get("hidden"):
                                default = model
                    cursor = page.get("nextCursor")
                    if not cursor:
                        return Catalog(configured if isinstance(configured, str) else None,
                                       models, default, isinstance(config, dict))
                    if not isinstance(cursor, str) or cursor == params.get("cursor"):
                        return None
                    params["cursor"] = cursor
            finally:
                # The metadata process has no turn and must not outlive the parent job.
                if proc.poll() is None:
                    detect._kill_group(proc)
    except (OSError, EOFError, subprocess.SubprocessError):
        return None


_UNSUPPORTED_MODEL = re.compile(
    r"\bmodel\b.*?\b(?:is not supported|is not available|does not exist)\b"
    r"|\b(?:unsupported model|model_not_supported|model_not_found)\b",
    re.IGNORECASE,
)


@dataclass
class Events:
    model_error: str = ""
    started: bool = False

    def observe(self, event: dict) -> bool:
        """Keep model rejections out of streamed output until the retry is decided."""
        kind = event.get("type")
        if kind in ("item.started", "item.completed", "turn.completed"):
            self.started = True
        if kind in ("error", "turn.failed"):
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else event.get("message")
            if isinstance(message, str) and _UNSUPPORTED_MODEL.search(message):
                self.model_error = message
                return True
        return False


@dataclass
class Attempt:
    rc: int
    stdout: str
    stderr: str
    timed_out: bool
    events: Events = field(default_factory=Events)

    @property
    def model_error(self) -> str:
        if not self.rc or self.timed_out:
            return ""
        return self.events.model_error or next(
            (line for line in self.stderr.splitlines() if _UNSUPPORTED_MODEL.search(line)), ""
        )


def run_bounded(argv: list[str], directory: str, last_message: str, timeout: float) -> Attempt:
    env = detect.agent_environment()

    def execute(args: list[str], remaining: float) -> Attempt:
        Path(last_message).unlink(missing_ok=True)
        attempt = Attempt(*detect._run_bounded(args, directory, remaining, env=env))
        for line in attempt.stdout.splitlines():
            if line.startswith("{"):
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    attempt.events.observe(event)
        return attempt

    attempt, _ = run(argv, directory, directory, env, timeout, execute)
    return attempt


def run(argv: list[str], cwd: str, directory: str, env: dict, timeout: float,
        call: Callable[[list[str], float], Attempt], *,
        fixed: str | None = None) -> tuple[Attempt, dict]:
    """Use the configured model when compatible; retry only a pre-work model rejection."""
    deadline = time.monotonic() + timeout

    def execute(model: str | None) -> Attempt:
        args = argv[:2] + ["--model", model] + argv[2:] if model else argv
        remaining = deadline - time.monotonic()
        return call(args, remaining) if remaining > 0 else Attempt(-1, "", "", True)

    catalog = read_catalog(argv[0], cwd, directory, env, min(4.0, timeout / 3)) if fixed is None else None
    requested = fixed or (catalog.configured if catalog else None)
    default = catalog.default if catalog else None
    override = requested
    if requested and catalog and requested not in catalog.models:
        override = default
    used = override or (default if catalog and catalog.config_known and not requested else None)
    attempt = execute(override)
    fallback = bool(requested and override and requested != override)
    if default and requested and default != used and attempt.model_error and not attempt.events.started:
        used = default
        attempt = execute(default)
        fallback = True
    return attempt, {"model": used, "requested_model": requested, "model_fallback": fallback}
