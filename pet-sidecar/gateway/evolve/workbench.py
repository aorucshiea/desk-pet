"""Candidate workbenches: isolated git worktrees for core code, and a
single-file staging area for plugins.

Safety decisions that are not obvious:
- Base is always ``git rev-parse HEAD``; the live dirty tree is never copied.
  An uncommitted edit on the user's machine can therefore never leak into a
  candidate (or be overwritten by a promotion).
- Edits are validated entirely in memory before any file is written, and all
  target paths are resolved (following symlinks) before a containment check.
  Test files are denied by name even though they live under allowed roots:
  the model must not be able to weaken the tests that judge its change.
- Written text is normalized to LF and restored to the file's original line
  ending. Windows checkouts are CRLF but the model reads numbered lines;
  matching raw CRLF would make every copied snippet fail.
"""

from __future__ import annotations

import ast
import difflib
import fnmatch
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .models import (
    MAX_TEXT_BYTES,
    NotAllowed,
    WorkbenchError,
    atomic_write_bytes,
    is_candidate_id,
    normalize_newlines,
    text_size_ok,
)
from .. import plugin_policy

PLUGIN_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}\.py$")

# Prefix + accepted suffixes. Explicit prefixes are used instead of
# PurePath.match("**/*.py") because ** does not cross separators there.
ALLOWED_ROOTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pet-sidecar/gateway/", (".py",)),
    ("pet-sidecar/deepseek_web/", (".py",)),
    ("pet-sidecar/laya_sidecar/", (".py", ".ps1")),
    ("clawd-on-desk/src/", (".js", ".css", ".html")),
    ("docs/", (".md",)),
)

DENIED_COMPONENTS = {
    "node_modules",
    ".git",
    "dist",
    "build",
    "__pycache__",
    "tests",
    "test",
}

DENIED_NAME_GLOBS = (
    ".env*",
    "*token*",
    "*.pem",
    "*.key",
    "id_rsa*",
)

# Test names are denied twice (component + filename) so a future tests
# directory inside an allowed root cannot be edited into passing.
TEST_NAME_GLOBS = (
    "test_*.py",
    "*_test.py",
    "*.test.js",
    "*.spec.js",
    "conftest.py",
)

LOCKFILE_NAMES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "uv.lock",
    "poetry.lock",
    "pipfile.lock",
    "cargo.lock",
    "composer.lock",
    "bun.lockb",
}

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
GIT_TIMEOUT = 120


def git_available() -> bool:
    return shutil.which("git") is not None


def _git(
    args: list[str],
    *,
    cwd: Path,
    timeout: float = GIT_TIMEOUT,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise WorkbenchError("找不到 git，无法建立隔离工作区") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorkbenchError(f"git {args[0]} 超时（{timeout:.0f}s）") from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise WorkbenchError(f"git {args[0]} 失败: {detail[:500]}")
    return proc


def normalize_rel_path(path: Any) -> str:
    """Validate a repo-relative path before it reaches the filesystem."""
    if not isinstance(path, str) or not path.strip():
        raise NotAllowed("编辑路径不能为空")
    if "\x00" in path:
        raise NotAllowed("编辑路径包含非法字符")
    raw = path.strip()
    if raw.startswith(("/", "\\")):
        raise NotAllowed(f"不允许绝对路径: {path}")
    try:
        if PurePosixPath(raw).is_absolute() or PureWindowsPath(raw).is_absolute():
            raise NotAllowed(f"不允许绝对路径: {path}")
    except ValueError as exc:
        raise NotAllowed(f"非法路径: {path}") from exc
    normalized = raw.replace("\\", "/")
    parts = normalized.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise NotAllowed(f"路径不能包含 . / .. 或空段: {path}")
    if any(":" in part for part in parts):
        raise NotAllowed(f"路径包含非法冒号: {path}")
    return "/".join(parts)


def denied_core_reason(rel: str) -> str:
    parts = rel.split("/")
    lowered = [part.lower() for part in parts]
    for part in lowered:
        if part in DENIED_COMPONENTS:
            return f"路径包含禁止的目录 '{part}'"
    name = lowered[-1]
    for pattern in TEST_NAME_GLOBS:
        if fnmatch.fnmatch(name, pattern):
            return f"不允许修改测试文件 '{name}'"
    for pattern in DENIED_NAME_GLOBS:
        if fnmatch.fnmatch(name, pattern):
            return f"不允许修改敏感文件 '{name}'"
    if name in LOCKFILE_NAMES:
        return f"不允许修改锁文件 '{name}'"
    return ""


def is_core_path_allowed(rel: str) -> bool:
    for prefix, suffixes in ALLOWED_ROOTS:
        if rel.startswith(prefix) and rel.lower().endswith(suffixes) and len(rel) > len(prefix):
            return True
    return False


def validate_core_path(path: Any) -> str:
    rel = normalize_rel_path(path)
    if not is_core_path_allowed(rel):
        raise NotAllowed(f"路径不在允许修改的范围内: {rel}")
    reason = denied_core_reason(rel)
    if reason:
        raise NotAllowed(f"{reason}: {rel}")
    return rel


def resolve_under(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``root`` and reject symlink escapes."""
    root_resolved = Path(root).resolve()
    target = (root_resolved / rel).resolve()
    if not target.is_relative_to(root_resolved):
        raise NotAllowed(f"路径逃逸出工作区: {rel}")
    return target


def read_source_text(path: Path) -> tuple[str, str]:
    """Return (LF-normalized text, original newline) or raise NotAllowed."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError as exc:
        raise WorkbenchError(f"文件不存在: {path.name}") from exc
    except OSError as exc:
        raise WorkbenchError(f"读取文件失败: {exc}") from exc
    if len(raw) > MAX_TEXT_BYTES:
        raise NotAllowed(f"文件过大（>{MAX_TEXT_BYTES // 1024} KB）: {path.name}")
    if b"\x00" in raw:
        raise NotAllowed(f"不允许修改二进制文件: {path.name}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NotAllowed(f"不允许修改非 UTF-8 文件: {path.name}") from exc
    newline = "\r\n" if "\r\n" in text else "\n"
    return normalize_newlines(text), newline


def write_source_text(path: Path, text: str, newline: str = "\n") -> None:
    text = normalize_newlines(text)
    if not text_size_ok(text):
        raise NotAllowed(f"写入内容过大（>{MAX_TEXT_BYTES // 1024} KB）: {Path(path).name}")
    data = text.replace("\n", "\r\n") if newline == "\r\n" else text
    atomic_write_bytes(Path(path), data.encode("utf-8"))


def _edit_shape(edits: list[dict], *, core: bool) -> list[dict[str, str]]:
    if not isinstance(edits, list) or not edits:
        raise NotAllowed("edits 不能为空")
    normalized: list[dict[str, str]] = []
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            raise NotAllowed(f"第 {index + 1} 条编辑格式无效")
        for key in ("path", "old", "new"):
            if key not in edit:
                raise NotAllowed(f"第 {index + 1} 条编辑缺少字段 '{key}'")
            if not isinstance(edit[key], str):
                raise NotAllowed(f"第 {index + 1} 条编辑的 '{key}' 必须是字符串")
        if edit["new"] == "":
            raise NotAllowed("M1 不支持删除文件或清空内容（new 不能为空）")
        path = validate_core_path(edit["path"]) if core else normalize_plugin_name(edit["path"])
        normalized.append({"path": path, "old": edit["old"], "new": edit["new"]})
    return normalized


def validate_core_edits(edits: list[dict]) -> list[dict[str, str]]:
    return _edit_shape(edits, core=True)


def normalize_plugin_name(name: Any) -> str:
    if not isinstance(name, str) or not PLUGIN_NAME_RE.fullmatch(name):
        raise NotAllowed(
            f"插件路径必须是不含目录的普通文件名（^[a-z][a-z0-9_]{{0,63}}\\.py$）: {name!r}"
        )
    return name


class Workbench:
    """Git worktree lifecycle for core candidates."""

    def __init__(self, repo_root: Path, data_dir: Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.data_dir = Path(data_dir).resolve()
        self.worktrees_dir = self.data_dir / "worktrees"
        self.baselines_dir = self.data_dir / "baselines"

    # ── repo state ────────────────────────────────────────────────────
    def repo_state(self) -> dict[str, Any]:
        if not git_available():
            raise WorkbenchError("找不到 git，无法读取仓库状态")
        sha = _git(["rev-parse", "HEAD"], cwd=self.repo_root).stdout.strip()
        if not sha:
            raise WorkbenchError("当前仓库还没有提交，无法建立演化基线")
        branch_proc = _git(
            ["rev-parse", "--abbrev-ref", "HEAD"], cwd=self.repo_root, check=False
        )
        branch = (branch_proc.stdout or "").strip() if branch_proc.returncode == 0 else ""
        status = _git(["status", "--porcelain"], cwd=self.repo_root).stdout
        dirty_files: list[str] = []
        for line in status.splitlines():
            if not line.strip():
                continue
            dirty_files.append(line[3:] if len(line) > 3 else line.strip())
        return {
            "base_commit": sha,
            "branch": branch,
            "base_dirty": bool(dirty_files),
            "dirty_files": dirty_files[:200],
        }

    def head_sha(self) -> str:
        return self.repo_state()["base_commit"]

    # ── worktree lifecycle ────────────────────────────────────────────
    def prepare(self, candidate_id: str) -> dict[str, Any]:
        if not is_candidate_id(candidate_id):
            raise WorkbenchError(f"候选 id 无效: {candidate_id!r}")
        if not git_available():
            raise WorkbenchError("找不到 git，无法建立候选工作区")
        state = self.repo_state()
        worktree = self.worktrees_dir / candidate_id
        if worktree.exists():
            raise WorkbenchError(f"候选工作区已存在: {candidate_id}")
        worktree.parent.mkdir(parents=True, exist_ok=True)
        _git(
            ["worktree", "add", "--detach", str(worktree), state["base_commit"]],
            cwd=self.repo_root,
        )
        try:
            # A branch (not just detached HEAD) keeps the candidate visible in
            # `git log --all` and makes inspection possible after a crash.
            _git(["checkout", "-b", f"evolve/{candidate_id}"], cwd=worktree)
        except BaseException:
            self._remove_worktree_path(worktree)
            raise
        return {
            **state,
            "worktree": str(worktree),
            "branch": f"evolve/{candidate_id}",
        }

    def apply_edits(self, worktree: Path, edits: list[dict]) -> list[str]:
        normalized = validate_core_edits(edits)
        root = Path(worktree)
        contents: dict[str, str] = {}
        originals: dict[str, str] = {}
        newlines: dict[str, str] = {}
        created: set[str] = set()
        changed: set[str] = set()
        targets: dict[str, Path] = {}

        for index, edit in enumerate(normalized):
            rel = edit["path"]
            target = resolve_under(root, rel)
            targets[rel] = target
            old = normalize_newlines(edit["old"])
            new = normalize_newlines(edit["new"])

            if edit["old"] == "":
                if rel in contents or target.exists():
                    raise NotAllowed(f"目标文件已存在，不能作为新文件创建: {rel}")
                contents[rel] = new
                originals[rel] = ""
                newlines[rel] = "\n"
                created.add(rel)
                changed.add(rel)
            else:
                if rel in contents:
                    current = contents[rel]
                else:
                    if not target.is_file():
                        raise WorkbenchError(f"目标文件不存在: {rel}")
                    current, newline = read_source_text(target)
                    originals[rel] = current
                    newlines[rel] = newline
                count = current.count(old)
                if count != 1:
                    raise WorkbenchError(
                        f"第 {index + 1} 条编辑的 old 片段在 {rel} 中出现 {count} 次（必须恰好 1 次）"
                    )
                updated = current.replace(old, new, 1)
                contents[rel] = updated
                if updated != originals.get(rel, current):
                    changed.add(rel)

            if not text_size_ok(contents[rel]):
                raise NotAllowed(f"编辑后内容过大（>{MAX_TEXT_BYTES // 1024} KB）: {rel}")

        if not changed:
            raise WorkbenchError("候选没有产生实际改动")

        written: list[tuple[str, Path]] = []
        try:
            for rel in sorted(changed):
                target = targets[rel]
                target.parent.mkdir(parents=True, exist_ok=True)
                write_source_text(target, contents[rel], newlines[rel])
                written.append((rel, target))
        except BaseException:
            # Leave the worktree clean even though the service will normally
            # remove it anyway; this also covers a future in-place apply mode.
            for rel, target in reversed(written):
                try:
                    if rel in created:
                        target.unlink()
                    else:
                        write_source_text(target, originals[rel], newlines[rel])
                except OSError:
                    pass
            raise
        return sorted(changed)

    def diff(self, worktree: Path, files: list[str]) -> str:
        if not files:
            raise WorkbenchError("没有可生成 diff 的文件")
        # Intent-to-add makes untracked new files visible to `git diff`.
        _git(["add", "-N", "--", *files], cwd=Path(worktree))
        proc = _git(["diff", "--no-color", "--", *files], cwd=Path(worktree))
        text = proc.stdout
        if not text.strip():
            raise WorkbenchError("候选 diff 为空")
        return text

    def discard(self, candidate_id: str, worktree: Path | str | None) -> None:
        if worktree:
            path = Path(worktree).resolve()
            # The record is on disk and could be tampered with; never let a
            # stale/hostile path point deletion at the live repo.
            if not path.is_relative_to(self.worktrees_dir.resolve()):
                raise WorkbenchError(f"拒绝删除候选工作区之外的路径: {worktree}")
            if path.exists():
                self._remove_worktree_path(path)
        # Best effort cleanup of the branch; the worktree is gone either way.
        _git(["branch", "-D", f"evolve/{candidate_id}"], cwd=self.repo_root, check=False)

    def _remove_worktree_path(self, path: Path) -> None:
        _git(["worktree", "remove", "--force", str(path)], cwd=self.repo_root, check=False)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        _git(["worktree", "prune"], cwd=self.repo_root, check=False)
        if path.exists():
            raise WorkbenchError(f"无法移除候选工作区: {path}")

    # ── baseline worktree (per base commit) ──────────────────────────
    def baseline_worktree(self, base_commit: str) -> Path:
        if not git_available():
            raise WorkbenchError("找不到 git，无法建立基线工作区")
        repo_key = hashlib.sha256(str(self.repo_root).encode("utf-8")).hexdigest()[:10]
        path = self.baselines_dir / f"{repo_key}-{base_commit[:12]}"
        if path.exists():
            proc = _git(["rev-parse", "HEAD"], cwd=path, check=False)
            if proc.returncode == 0 and proc.stdout.strip() == base_commit:
                return path
            self._remove_worktree_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _git(["worktree", "add", "--detach", str(path), base_commit], cwd=self.repo_root)
        return path


def plugin_candidate_path(candidate_dir: Path, name: str) -> Path:
    return Path(candidate_dir) / "plugin" / normalize_plugin_name(name)


def prepare_plugin_candidate(
    candidate_dir: Path,
    live_dir: Path,
    edits: list[dict],
) -> dict[str, Any]:
    """Stage a plugin candidate under candidates/<id>/plugin/.

    Returns source text, original line ending, synthetic diff and the path.
    Policy + syntax are checked here (propose-time rejection) and again in
    the evaluator, which is the last line of defence before a live write.
    """
    normalized = _edit_shape(edits, core=False)
    names = {edit["path"] for edit in normalized}
    if len(names) != 1:
        raise NotAllowed("一个插件候选只能修改一个插件文件")
    name = names.pop()
    live_dir = Path(live_dir)
    live_path = live_dir / name

    if any(not edit["old"] for edit in normalized) and live_path.exists():
        raise NotAllowed(f"文件已存在，不能作为新插件创建: {name}")
    if any(edit["old"] for edit in normalized) and not live_path.exists():
        raise WorkbenchError(f"目标插件不存在: {name}")

    base_text = ""
    newline = "\n"
    if live_path.exists():
        if not live_path.is_file():
            raise NotAllowed(f"插件路径不是文件: {name}")
        base_text, newline = read_source_text(live_path)

    current = base_text
    for index, edit in enumerate(normalized):
        old = normalize_newlines(edit["old"])
        new = normalize_newlines(edit["new"])
        if edit["old"] == "":
            if current:
                raise NotAllowed(f"文件已存在，不能作为新插件创建: {name}")
            current = new
        else:
            count = current.count(old)
            if count != 1:
                raise WorkbenchError(
                    f"第 {index + 1} 条编辑的 old 片段在 {name} 中出现 {count} 次（必须恰好 1 次）"
                )
            current = current.replace(old, new, 1)

    if not current.strip():
        raise WorkbenchError("插件候选为空")
    if not text_size_ok(current):
        raise NotAllowed(f"插件源码过大（>{MAX_TEXT_BYTES // 1024} KB）")

    violations = plugin_policy.check_source(current)
    if violations:
        raise NotAllowed(
            "插件策略拒绝: " + plugin_policy.format_violations(violations)
        )
    try:
        ast.parse(current)
    except SyntaxError as exc:
        raise NotAllowed(f"插件语法错误（第 {exc.lineno} 行）: {exc.msg}") from exc

    if current == base_text:
        raise WorkbenchError("候选没有产生实际改动")

    target = plugin_candidate_path(candidate_dir, name)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Candidate storage is canonical LF; the live file's original line ending
    # is restored on promotion.
    write_source_text(target, current, "\n")

    diff_lines = list(
        difflib.unified_diff(
            base_text.splitlines(keepends=True),
            current.splitlines(keepends=True),
            fromfile=f"a/plugins/{name}",
            tofile=f"b/plugins/{name}",
            lineterm="\n",
        )
    )
    diff = "".join(line if line.endswith("\n") else line + "\n" for line in diff_lines)
    if not diff.strip():
        raise WorkbenchError("插件候选 diff 为空")
    return {
        "plugin_name": name,
        "plugin_path": str(target),
        "plugin_text": current,
        "plugin_newline": newline,
        "diff": diff,
        "files_changed": [f"plugins/{name}"],
    }


__all__ = [
    "Workbench",
    "git_available",
    "is_core_path_allowed",
    "normalize_plugin_name",
    "normalize_rel_path",
    "plugin_candidate_path",
    "prepare_plugin_candidate",
    "read_source_text",
    "resolve_under",
    "validate_core_edits",
    "validate_core_path",
    "write_source_text",
]
