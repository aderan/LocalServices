#!/usr/bin/env python3
"""N00 复现脚本（MB-LOCAL-SERVICES-LIFECYCLE-V2 §2 遗留项）。

在完全隔离的临时 LOCALSERVICES_HOME 中逐条复现上轮复核遗留问题；
只记录行为，不判定修复，不触碰真实登记/launchd/容器。
用法：python3 scripts/n00_repro_v2.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

RESULTS: list[tuple[str, str, str]] = []  # (id, verdict, evidence)


def record(item: str, reproduced: bool, evidence: str) -> None:
    RESULTS.append((item, "REPRODUCED" if reproduced else "NOT-REPRODUCED", evidence))
    print(f"[{item}] {'REPRODUCED' if reproduced else 'NOT-REPRODUCED'}\n  {evidence}\n")


class Env:
    """每项独立隔离的 LOCALSERVICES_HOME / 内置与用户清单目录。"""

    def __init__(self, builtin_tomls: dict[str, str] | None = None):
        self.tmp = Path(tempfile.mkdtemp(prefix="ls-n00-"))
        (self.tmp / "s").mkdir()
        (self.tmp / "u").mkdir()
        (self.tmp / "h").mkdir()
        for name, text in (builtin_tomls or {}).items():
            (self.tmp / "s" / name).write_text(text, encoding="utf-8")
        self.old = {k: os.environ.get(k) for k in
                    ("LOCALSERVICES_HOME", "LOCALSERVICES_SERVICES", "LOCALSERVICES_USER_SERVICES")}
        os.environ["LOCALSERVICES_HOME"] = str(self.tmp / "h")
        os.environ["LOCALSERVICES_SERVICES"] = str(self.tmp / "s")
        os.environ["LOCALSERVICES_USER_SERVICES"] = str(self.tmp / "u")

    def close(self):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)


def payload(sid: str, source: Path, revision: int = 0, name: str | None = None,
            description: str | None = None, port: int = 18100, cmd: list[str] | None = None,
            project_id: str = "p1") -> dict:
    svc: dict = {"id": sid, "name": name or sid, "source": str(source),
                 "components": [{"id": "web", "kind": "server",
                                 "cmd": cmd or [sys.executable, "-m", "http.server"],
                                 "cwd": ".", "port": port}]}
    if description is not None:
        svc["description"] = description
    return {"schemaVersion": 1, "revision": revision,
            "project": {"id": project_id, "name": "P1"}, "service": svc}


def main() -> int:
    from localservices import registry as R
    from localservices.manifest import Project

    # ---------- ① 持续 I/O 故障：回滚依赖再次分配空间写旧文件 ----------
    env = Env()
    try:
        (env.tmp / "dirA").mkdir()
        reg = R.Registry()
        reg.register_service(payload("demo", env.tmp / "dirA", port=18101))
        toml = env.tmp / "u" / "demo.toml"
        original = toml.read_bytes()
        new_payload = payload("demo", env.tmp / "dirA", revision=1, port=18101,
                              cmd=[sys.executable, "-m", "http.server", "directory"])
        calls = {"n": 0}
        orig_write = R._write_bytes_atomic

        def failing_write(path: Path, data: bytes) -> None:
            calls["n"] += 1
            if calls["n"] >= 2:  # 第 1 次=TOML 新内容；之后（registry 保存与回滚）全部失败
                raise OSError("simulated persistent disk-full")
            return orig_write(path, data)

        R._write_bytes_atomic = failing_write
        raised = None
        try:
            reg.register_service(new_payload)
        except Exception as e:  # noqa: BLE001
            raised = e
        R._write_bytes_atomic = orig_write
        toml_after = toml.read_bytes()
        reg_after = json.loads((env.tmp / "h" / "registry.json").read_text())
        inconsistent = raised is not None and toml_after != original
        record("1-persistent-io-rollback", inconsistent,
               f"register 抛 {type(raised).__name__} 后 TOML {'已变成新内容（回滚失败）' if toml_after != original else '保持原样'}；"
               f"registry.json revision={reg_after.get('revision')}（旧值），TOML 与登记不一致")
    finally:
        env.close()

    # ---------- ② 结构坏 JSON / 非 dict / 读失败被当空库 ----------
    env = Env()
    try:
        (env.tmp / "s0").mkdir()
        rf = env.tmp / "h" / "registry.json"
        rf.parent.mkdir(parents=True, exist_ok=True)
        rf.write_text("[1, 2, 3]", encoding="utf-8")  # 合法 JSON 但不是对象
        reg = R.Registry()
        reg.register_service(payload("demo", env.tmp / "s0", port=18102))
        backups = list((env.tmp / "h").glob("registry.json.corrupt-*"))
        overwritten = rf.read_text().startswith("{")
        record("2a-array-json-silent-empty", overwritten and not backups,
               f"非 dict JSON 被静默当空库；后续保存直接覆盖原文件（overwritten={overwritten}），"
               f"corrupt 备份={len(backups)} 个")
    finally:
        env.close()

    env = Env()
    try:
        rf = env.tmp / "h" / "registry.json"
        rf.parent.mkdir(parents=True, exist_ok=True)
        rf.write_text('{"schemaVersion": 1, "projects": [{"id": "keep", "name": "K"}]}', encoding="utf-8")
        os.chmod(rf, 0)
        reg = R.Registry()
        data = reg.data()
        os.chmod(rf, 0o644)
        record("2b-unreadable-silent-empty", data.get("projects") == [] and rf.read_text().startswith("{"),
               "registry.json 无读权限时 _load 静默返回空库（projects==[]），原文件仍在：下次保存将无备份覆盖真实记录")
    finally:
        env.close()

    # ---------- ③ dry_run 不完整配置仍落草稿 ----------
    env = Env()
    try:
        reg = R.Registry()
        res = reg.register_service(
            {"schemaVersion": 1, "revision": 0, "project": {"id": "p1", "name": "P1"},
             "service": {"id": "half", "name": "Half", "source": str(env.tmp),
                         "components": [{"id": "web", "cwd": "."}]}},  # 缺 cmd：不完整
            dry_run=True)
        drafts = list((env.tmp / "h" / "drafts").glob("*.json"))
        record("3-dryrun-writes-draft", bool(drafts),
               f"dry_run=true 的不完整提交 action={res.get('action')}，仍写入草稿 {len(drafts)} 个"
               f"（{drafts[0].name if drafts else '-'}）；dry_run 分支含草稿写入")
    finally:
        env.close()

    # ---------- ④ 人工覆盖字段在重新识别时丢失 ----------
    env = Env()
    try:
        (env.tmp / "dirA").mkdir()
        reg = R.Registry()
        reg.register_service(payload("demo", env.tmp / "dirA", name="我的名字", description="手工描述",
                                     port=18104))
        # 重新识别返回的候选带了新的 name/description（GUI 恒携带这两个字段）
        res = reg.register_service(payload("demo", env.tmp / "dirA", revision=1, name="识别名",
                                           description="", port=18104))
        toml = (env.tmp / "u" / "demo.toml").read_text()
        lost = 'name = "识别名"' in toml and 'description = ""' in toml
        record("4-manual-overrides-lost", lost,
               f"更新 action={res.get('action')} 后 TOML name/description 被识别值替换"
               f"（用户手工名/描述丢失，无覆盖层保留机制）")
    finally:
        env.close()

    # ---------- ⑤ 多目录同名候选：第二次注册覆盖第一次 ----------
    env = Env()
    try:
        (env.tmp / "dirA").mkdir()
        (env.tmp / "dirB").mkdir()
        reg = R.Registry()
        r1 = reg.register_service(payload("demo", env.tmp / "dirA", port=18105))
        r2 = reg.register_service(payload("demo", env.tmp / "dirB", revision=1, port=18105))
        toml = (env.tmp / "u" / "demo.toml").read_text()
        overwritten = str(env.tmp / "dirB") in toml and str(env.tmp / "dirA") not in toml
        record("5-multi-dir-same-id-overwrite", r1.get("action") == "created" and
               r2.get("action") == "updated" and overwritten,
               f"dirA 注册 {r1.get('action')} 后，dirB 以同 id 注册 {r2.get('action')} 直接覆盖；"
               f"TOML source 已指向 dirB，dirA 登记无声消失")
    finally:
        env.close()

    # ---------- ⑥ 自动启动：缺 name 返回 400；项目级偏好未进引擎 ----------
    env = Env()
    try:
        reg = R.Registry()
        reg.ensure_project("p1", "P1", None, None)  # GUI toggleAutostart 只发 {id, autostart}
        err = None
        try:
            reg.ensure_project("p1", None, None, None, autostart=True)
        except R.RegistryError as e:
            err = str(e)
        record("6a-autostart-missing-name-400", err is not None and "name" in err,
               f"ensure_project(pid, name=None, autostart=True) → RegistryError: {err}；"
               f"GUI 自动开关联动 400")
    finally:
        env.close()

    env = Env()
    try:
        reg = R.Registry()
        proj, _ = reg.ensure_project("p1", "P1", None, None, autostart=True)  # 项目级 autostart=true
        from localservices.controller import Store
        store = Store()
        svc = Project(id="demo", name="demo", source=env.tmp, autostart=False)  # 服务级未开
        intent = store.intent(svc)
        record("6b-project-autostart-not-in-engine", intent == "stopped",
               f"registry 项目 autostart=true，但 Store.intent 默认只读服务 TOML autostart=false → "
               f"intent={intent!r}；项目级偏好写而不生效")
    finally:
        env.close()

    # ---------- ⑦ 不同 ID 同身份：dry_run diff 与实际 duplicate 不一致 ----------
    env = Env()
    try:
        (env.tmp / "dirA").mkdir()
        reg = R.Registry()
        reg.register_service(payload("svc-a", env.tmp / "dirA", port=18107))
        d = reg.register_service(payload("svc-b", env.tmp / "dirA", port=18107), dry_run=True)
        real = reg.register_service(payload("svc-b", env.tmp / "dirA", port=18107))
        mismatch = d.get("action") == "diff" and real.get("action") == "duplicate"
        record("7-dup-identity-diff-mismatch", mismatch,
               f"dry_run action={d.get('action')!r}（暗示将写入 diff），实际 action={real.get('action')!r} "
               f"service_id={real.get('service_id')!r} 不写入；预览与实际不一致")
    finally:
        env.close()

    # ---------- ⑧ 移除内置服务直接删除仓内模板 ----------
    builtin = """id = "builtin"
name = "Builtin"
source = "%s"
project = "p1"

[[components]]
id = "web"
kind = "server"
cmd = ["/usr/bin/true"]
cwd = "."
port = 18108
"""
    env = Env({"builtin.toml": builtin % str(env.tmp)})
    try:
        reg = R.Registry()
        reg.ensure_project("p1", "P1", None, None)
        (env.tmp / "dirA").mkdir()
        reg.register_service(payload("dummy", env.tmp / "dirA", port=18109))
        reg.remove_service("builtin", "keep-external", None)
        gone = not (env.tmp / "s" / "builtin.toml").exists()
        record("8-builtin-unlink", gone,
               "remove_service 对内置目录清单同样执行 unlink：services/builtin.toml（随仓分发的模板）被直接删除；"
               "无 tombstone/用户层标记机制")
    finally:
        env.close()

    print("=" * 72)
    rep = sum(1 for _, v, _ in RESULTS if v == "REPRODUCED")
    print(f"共 {len(RESULTS)} 项，复现 {rep} 项")
    for item, verdict, _ in RESULTS:
        print(f"  {verdict:15s} {item}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
