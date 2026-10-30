#!/usr/bin/env python3
"""组装并自检 LocalServices 注册 payload（SKILL.md 第 2 步的辅助工具）。

用法：
    build_payload.py [--source DIR] [--project-id ID] [--project-name NAME]
                     [--agent codex|claude|none] [--draft] < partial.json

- stdin 收 partial JSON（最小只需 service.name / service.source / components[].id+cmd），
  做最小拼装（补 schemaVersion、--source/--project-* 旗标）与结构自检后，
  把规范化 payload 打到 stdout。
- 结构错误（cmd 不是 argv 数组、id 非法、未知字段、端口越界……）列在 stderr，退出码 2，
  不输出 payload——修好再注册。
- 缺必要字段（name/source/组件 cmd）时自动置 service.draft=true（服务会存为草稿），
  缺失项列在 stderr，退出码 3，payload 照常输出。

退出码：0 完整可注册；3 不完整（草稿）；2 结构错误。只依赖标准库；
本工具不做端口冲突 / cmd[0] 可解析等需要环境的校验——那是 daemon 注册时的事。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
COMPONENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
AGENTS = ("codex", "claude", "none")
KINDS = ("server", "worker", "generator")
SERVICE_KEYS = {"id", "name", "description", "source", "open", "autostart", "adapter",
                "observe_only", "draft", "project_hint", "components", "dependencies", "evidence"}
COMP_KEYS = {"id", "kind", "name", "cmd", "shell_script", "cwd", "env", "env_files", "port", "ready",
             "required", "start_timeout", "after", "hot", "watch", "restart_on", "ignore", "build",
             "install", "install_when", "install_marker", "auto_update", "external_cmd",
             "external_cwd", "external_cwd_exact", "debounce"}
DEP_KEYS = {"name", "probe", "affects", "managed", "note", "required", "container", "volume", "resource"}


def check(payload: dict, opts) -> list[str]:
    """原地补默认值；返回结构错误列表（非空则拒绝输出）。"""
    errs: list[str] = []
    if not isinstance(payload, dict):
        return ["顶层必须是 JSON 对象"]
    payload.setdefault("schemaVersion", 1)
    if payload["schemaVersion"] != 1:
        errs.append("schemaVersion 必须为 1")
    rev = payload.get("revision", 0)
    if isinstance(rev, bool) or not isinstance(rev, int) or rev < 0:
        errs.append("revision 必须为非负整数")
    if "revision" not in payload:
        payload["revision"] = 0

    proj = payload.get("project")
    if proj is not None and not isinstance(proj, dict):
        errs.append("project 必须是对象")
        proj = None
    if proj is None and (opts.project_id or opts.project_name or opts.agent):
        proj = payload["project"] = {}
    if proj is not None:
        pid = proj.get("id")
        if pid is not None and (not isinstance(pid, str) or not ID_RE.fullmatch(pid)):
            errs.append(f"project.id 非法（须匹配 {ID_RE.pattern}）：{pid!r}")
        if opts.project_id and "id" not in proj:
            proj["id"] = opts.project_id
        if opts.project_name and not str(proj.get("name", "")).strip():
            proj["name"] = opts.project_name
        agent = proj.get("agent")
        if agent is not None and agent not in AGENTS:
            errs.append(f"project.agent 必须是 {'/'.join(AGENTS)}：{agent!r}")
        if opts.agent and "agent" not in proj:
            proj["agent"] = opts.agent

    dirs_ = payload.get("directories")
    if dirs_ is not None:
        if not isinstance(dirs_, list):
            errs.append("directories 必须是数组")
        else:
            for i, d in enumerate(dirs_):
                if not isinstance(d, dict) or not isinstance(d.get("path"), str) or not d["path"].strip():
                    errs.append(f"directories[{i}].path 必须是非空字符串")

    svc = payload.get("service")
    if not isinstance(svc, dict):
        errs.append("service 必须是对象（缺少则无法注册，连草稿也存不了）")
        return errs
    unknown = set(svc) - SERVICE_KEYS
    if unknown:
        errs.append(f"service 含未知字段：{sorted(unknown)}")
    sid = svc.get("id")
    if sid is not None and (not isinstance(sid, str) or not ID_RE.fullmatch(sid)):
        errs.append(f"service.id 非法（须匹配 {ID_RE.pattern}）：{sid!r}")
    if opts.source and not str(svc.get("source", "")).strip():
        svc["source"] = str(Path(opts.source).expanduser())

    comps = svc.get("components")
    comps = comps if isinstance(comps, list) else []
    for i, c in enumerate(comps):
        if not isinstance(c, dict):
            errs.append(f"components[{i}] 必须是对象")
            continue
        cu = set(c) - COMP_KEYS
        if cu:
            errs.append(f"components[{i}] 含未知字段：{sorted(cu)}")
        cid = c.get("id")
        if not isinstance(cid, str) or not COMPONENT_ID_RE.fullmatch(cid or ""):
            errs.append(f"components[{i}].id 非法（须匹配 {COMPONENT_ID_RE.pattern}）：{cid!r}")
        kind = c.get("kind")
        if kind is not None and kind not in KINDS:
            errs.append(f"components[{i}].kind 必须是 {'/'.join(KINDS)}：{kind!r}")
        cmd = c.get("cmd")
        if cmd is not None:
            if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) and x.strip() for x in cmd):
                errs.append(f"components[{i}].cmd 必须是非空 argv 数组（如 [\"make\", \"serve\"]），"
                            f"不接受拼接字符串：{cmd!r}")
        if not (isinstance(c.get("shell_script"), str) and c["shell_script"].strip()) \
                and not (isinstance(cmd, list) and cmd):
            if "cmd" in c or "shell_script" in c:
                errs.append(f"components[{i}].cmd 与 shell_script 至少一个有效")
        port = c.get("port")
        if port is not None and (isinstance(port, bool) or not isinstance(port, int)
                                 or not 1 <= port <= 65535):
            errs.append(f"components[{i}].port 必须是 1–65535 整数：{port!r}")
        env = c.get("env")
        if env is not None and (not isinstance(env, dict)
                                or not all(isinstance(k, str) and isinstance(v, str)
                                           for k, v in env.items())):
            errs.append(f"components[{i}].env 必须是 string→string 对象")

    deps = svc.get("dependencies")
    if deps is not None:
        if not isinstance(deps, list):
            errs.append("dependencies 必须是数组")
        else:
            for i, d in enumerate(deps):
                if not isinstance(d, dict):
                    errs.append(f"dependencies[{i}] 必须是对象")
                    continue
                du = set(d) - DEP_KEYS
                if du:
                    errs.append(f"dependencies[{i}] 含未知字段：{sorted(du)}")
                if not isinstance(d.get("name"), str) or not str(d.get("name", "")).strip():
                    errs.append(f"dependencies[{i}].name 必须是非空字符串")
                if not isinstance(d.get("probe"), dict):
                    errs.append(f"dependencies[{i}].probe 必须是对象（http/tcp/json_ok/timeout）")
    return errs


def incomplete_reasons(payload: dict) -> list[str]:
    svc = payload.get("service")
    if not isinstance(svc, dict):
        return ["缺 service"]
    why = []
    if not str(svc.get("name", "")).strip():
        why.append("service.name")
    if not str(svc.get("source", "")).strip():
        why.append("service.source")
    comps = svc.get("components")
    comps = comps if isinstance(comps, list) else []
    if not comps:
        why.append("service.components 为空")
    for i, c in enumerate(comps):
        if not isinstance(c, dict):
            continue
        has_cmd = isinstance(c.get("cmd"), list) and c["cmd"]
        has_script = isinstance(c.get("shell_script"), str) and c["shell_script"].strip()
        if not (has_cmd or has_script):
            why.append(f"components[{i}] 缺 cmd（argv 数组）或 shell_script")
    return why


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", help="工程目录；service.source 缺失时填入")
    ap.add_argument("--project-id", help="project.id 缺失时填入")
    ap.add_argument("--project-name", help="project.name 缺失时填入")
    ap.add_argument("--agent", choices=AGENTS, help="project.agent 缺失时填入")
    ap.add_argument("--draft", action="store_true", help="强制 service.draft=true（存草稿）")
    opts = ap.parse_args()

    try:
        payload = json.loads(sys.stdin.read())
    except ValueError as e:
        print(f"stdin 不是合法 JSON：{e}", file=sys.stderr)
        return 2

    errs = check(payload, opts)
    if errs:
        print("结构错误（修正后再注册，服务端同样会拒绝）：", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        return 2

    if opts.draft:
        payload["service"]["draft"] = True
    why = incomplete_reasons(payload)
    if why and not payload["service"].get("draft"):
        payload["service"]["draft"] = True
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if why:
        print("不完整（已标记 draft=true，注册后会存为草稿，缺：", file=sys.stderr)
        print("  " + "；".join(why) + "）", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
