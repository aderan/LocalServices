"""Source change detection by working-tree mtimes (captures uncommitted saves, unlike git SHA)."""

from __future__ import annotations

import fnmatch
import hashlib
import os
from pathlib import Path

SKIP_DIRS = {
    "node_modules", ".git", ".next", "dist", "build", "__pycache__", ".venv", "venv",
    ".turbo", ".cache", ".pytest_cache", ".mypy_cache", "coverage", ".vite", "target",
}


def _ignored(rel: str, ignore: list[str]) -> bool:
    for pat in ignore:
        if rel == pat or rel.startswith(pat.rstrip("/") + "/") or fnmatch.fnmatch(rel, pat):
            return True
    return False


def newest(root: Path, watch: list[str], ignore: list[str] | None = None, limit: int = 200_000) -> tuple[float, str]:
    """Newest mtime among files under root/<watch entries>. Returns (mtime, relpath)."""
    ignore = ignore or []
    best_t, best_p = 0.0, ""
    count = 0
    for entry in watch:
        base = root / entry
        if "*" in entry:
            candidates = list(root.glob(entry))
        else:
            candidates = [base]
        for cand in candidates:
            if cand.is_file():
                rel = str(cand.relative_to(root))
                if _ignored(rel, ignore):
                    continue
                t = cand.stat().st_mtime
                if t > best_t:
                    best_t, best_p = t, rel
                continue
            stack = [cand]
            while stack:
                d = stack.pop()
                try:
                    # a directory's mtime moves when a file in it is deleted or renamed
                    t = d.stat().st_mtime
                    if t > best_t:
                        best_t, best_p = t, os.path.relpath(d, root) + "/"
                    it = os.scandir(d)
                except OSError:
                    continue
                with it:
                    for e in it:
                        name = e.name
                        if name.startswith(".") and name not in (".env", ".env.local"):
                            if e.is_dir(follow_symlinks=False):
                                continue
                        try:
                            if e.is_dir(follow_symlinks=False):
                                if name in SKIP_DIRS:
                                    continue
                                rel = os.path.relpath(e.path, root)
                                if not _ignored(rel, ignore):
                                    stack.append(Path(e.path))
                                continue
                            rel = os.path.relpath(e.path, root)
                            if _ignored(rel, ignore):
                                continue
                            t = e.stat(follow_symlinks=False).st_mtime
                        except OSError:
                            continue
                        count += 1
                        if t > best_t:
                            best_t, best_p = t, rel
                        if count > limit:
                            return best_t, best_p
    return best_t, best_p


def files_hash(root: Path, files: list[str]) -> str:
    h = hashlib.sha256()
    for f in files:
        p = root / f
        h.update(f.encode())
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"<missing>")
    return h.hexdigest()[:16]
