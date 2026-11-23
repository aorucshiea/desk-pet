"""Model-facing inspection: a compact text map of the repo and its plugins.

This is deliberately text, not JSON: the model reads it in a prompt. Plugin
roster lines come from AST parsing, never importing plugin files — an organ
may have side effects on import, and inspect must be safe to call at any time.
"""

from __future__ import annotations

import ast
import fnmatch
from pathlib import Path

from .models import (
    NotAllowed,
    NotFound,
    WorkbenchError,
)
from .store import EvolveStore
from .workbench import (
    DENIED_COMPONENTS,
    DENIED_NAME_GLOBS,
    LOCKFILE_NAMES,
    TEST_NAME_GLOBS,
    Workbench,
    is_core_path_allowed,
    read_source_text,
)

MAX_READ_LINES = 400
MAX_READ_BYTES = 64 * 1024
TREE_ENTRY_CAP = 400


class Inspector:
    def __init__(
        self,
        repo_root: Path,
        store: EvolveStore,
        plugin_dir: Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.store = store
        self.plugin_dir = Path(plugin_dir).resolve() if plugin_dir else None

    # ── report ───────────────────────────────────────────────────────
    def report(self, *, max_files: int = 400) -> str:
        max_files = max(1, min(int(max_files), 5000))
        lines: list[str] = []
        lines.append("# Desk Pet self-evolution workbench (M1)")
        lines.append("")

        workbench = Workbench(self.repo_root, self.store.data_dir)
        try:
            state = workbench.repo_state()
            short = state["base_commit"][:7]
            lines.append(f"HEAD {short}  branch={state['branch'] or '(detached)'}  "
                         f"dirty_files={len(state['dirty_files'])}")
            if state["dirty_files"]:
                shown = ", ".join(state["dirty_files"][:8])
                lines.append(f"  dirty: {shown}")
        except WorkbenchError as exc:
            lines.append(f"HEAD unavailable: {exc}")
        lines.append("")

        lines.append("## tree")
        gateway = self.repo_root / "pet-sidecar" / "gateway"
        lines.extend(self._tree(gateway, "pet-sidecar/gateway/", max_files))
        lines.append("")
        lines.extend(self._top_level(self.repo_root / "pet-sidecar" / "deepseek_web",
                                     "pet-sidecar/deepseek_web/", min(80, max_files)))
        lines.extend(self._top_level(self.repo_root / "clawd-on-desk" / "src",
                                     "clawd-on-desk/src/", min(80, max_files)))
        lines.append("")

        lines.append("## gateway modules (top 30 by lines)")
        lines.extend(self._module_sizes(gateway, 30))
        lines.append("")

        lines.append("## tests")
        python = self._python_label()
        lines.append(f"  full:    cd pet-sidecar && {python} -m pytest -q")
        lines.append(f"  focused: cd pet-sidecar && {python} -m pytest -q tests/test_<module>.py")
        lines.append("")

        lines.append("## plugins")
        lines.extend(self._plugin_roster())
        lines.append("")

        lines.append("## data")
        lines.append(f"  data_dir: {self.store.data_dir}")
        lines.append(f"  candidates: {self.store.count_candidates()}")
        lines.append(f"  worktrees: {self.store.worktrees_dir}")
        lines.append(f"  backups:   {self.store.backups_dir}")
        lines.append("  lineage (last 5):")
        tail = self.store.lineage_tail(5)
        if not tail:
            lines.append("    (empty)")
        for item in tail:
            lines.append(
                "    {ts} {event} {id} status={status} actor={actor} diff={diff}".format(
                    ts=str(item.get("ts", ""))[:19],
                    event=item.get("event", "?"),
                    id=item.get("id", "?"),
                    status=item.get("status", "-"),
                    actor=item.get("actor", "-"),
                    diff=str(item.get("diff_sha256", ""))[:8],
                )
            )
        lines.append("")
        lines.append("## M1 rules")
        lines.append("  core edits: worktree only, review-only (promote refused)")
        lines.append("  plugin edits: policy + smoke test, then human-approved promote/rollback")
        return "\n".join(lines).strip() + "\n"

    def _python_label(self) -> str:
        for rel in ("pet-sidecar/.venv/Scripts/python.exe", "pet-sidecar/.venv/bin/python"):
            if (self.repo_root / rel).is_file():
                return rel.split("pet-sidecar/", 1)[-1]
        return "python"

    def _tree(self, root: Path, label: str, budget: int) -> list[str]:
        if not root.is_dir():
            return [f"{label} (missing)"]
        out = [f"{label} (recursive, max {budget} entries)"]
        count = 0

        def walk(directory: Path, indent: str) -> None:
            nonlocal count
            if count >= budget:
                return
            try:
                entries = sorted(
                    directory.iterdir(),
                    key=lambda p: (not p.is_dir(), p.name.lower()),
                )
            except OSError:
                return
            for path in entries:
                if count >= budget:
                    return
                name = path.name
                if name in ("__pycache__", ".git", ".logs") or name.startswith("."):
                    continue
                if path.is_dir():
                    out.append(f"{indent}{name}/")
                    count += 1
                    walk(path, indent + "  ")
                elif path.suffix == ".py":
                    out.append(f"{indent}{name}")
                    count += 1

        walk(root, "  ")
        if count >= budget:
            out.append(f"  ... (truncated at {budget} entries)")
        return out

    def _top_level(self, root: Path, label: str, cap: int) -> list[str]:
        if not root.is_dir():
            return [f"{label} (missing)"]
        try:
            entries = sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError as exc:
            return [f"{label} (unreadable: {exc})"]
        files = [p for p in entries if p.is_file()]
        dirs = [p for p in entries if p.is_dir()]
        out = [f"{label} ({len(files)} files, {len(dirs)} dirs)"]
        shown = 0
        for path in entries:
            if shown >= cap:
                out.append(f"  ... (truncated at {cap})")
                break
            if path.is_dir():
                out.append(f"  {path.name}/")
            elif path.suffix in (".py", ".js", ".ts", ".css", ".html", ".json", ".md"):
                out.append(f"  {path.name}")
            else:
                continue
            shown += 1
        return out

    def _module_sizes(self, gateway: Path, limit: int) -> list[str]:
        if not gateway.is_dir():
            return ["  (missing)"]
        rows: list[tuple[int, str]] = []
        for path in sorted(gateway.glob("*.py")):
            try:
                with path.open("rb") as fh:
                    rows.append((sum(1 for _ in fh), path.name))
            except OSError:
                continue
        rows.sort(key=lambda item: (-item[0], item[1]))
        return [f"  {count:6d}  {name}" for count, name in rows[:limit]]

    def _plugin_roster(self) -> list[str]:
        if self.plugin_dir is None:
            return ["  (plugin_dir not configured)"]
        if not self.plugin_dir.is_dir():
            return [f"  (missing: {self.plugin_dir})"]
        out: list[str] = []
        for path in sorted(self.plugin_dir.glob("*.py")):
            try:
                source = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                out.append(f"  {path.name}: unreadable ({exc})")
                continue
            try:
                tree = ast.parse(source)
            except SyntaxError as exc:
                out.append(f"  {path.name}: syntax error line {exc.lineno}")
                continue
            tools: list[str] = []
            inject: list[str] = []
            has_apply = False
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "apply":
                    has_apply = True
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr == "tool" and node.args:
                        first = node.args[0]
                        if isinstance(first, ast.Constant) and isinstance(first.value, str):
                            tools.append(first.value)
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "inject":
                            if isinstance(node.value, (ast.List, ast.Tuple)):
                                for elt in node.value.elts:
                                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                        inject.append(elt.value)
            if not has_apply:
                out.append(f"  {path.name}: no apply(ctx)")
                continue
            out.append(
                f"  {path.name}: tools=[{', '.join(tools) or '-'}]"
                + (f" inject=[{', '.join(inject)}]" if inject else "")
            )
        return out or ["  (no plugin files)"]

    # ── read_source ──────────────────────────────────────────────────
    def read_source(self, path: str, *, start: int = 1, end: int | None = None) -> str:
        if not isinstance(path, str) or not path.strip():
            raise NotAllowed("路径不能为空")
        if not isinstance(start, int) or start < 1:
            raise NotAllowed("start 必须是从 1 开始的行号")
        if end is not None and (not isinstance(end, int) or end < start):
            raise NotAllowed("end 必须大于等于 start")

        raw = Path(path)
        try:
            resolved = (raw if raw.is_absolute() else self.repo_root / raw).resolve()
        except OSError as exc:
            raise NotFound(f"无法解析路径: {path}") from exc
        if not resolved.is_relative_to(self.repo_root):
            raise NotAllowed(f"只能读取仓库内的文件: {path}")
        if not resolved.is_file():
            raise NotFound(f"文件不存在: {path}")
        rel = resolved.relative_to(self.repo_root).as_posix()
        if not self._readable(rel):
            raise NotAllowed(f"路径不在允许读取的范围内: {rel}")
        try:
            text, _ = read_source_text(resolved)
        except WorkbenchError as exc:
            raise NotFound(str(exc)) from exc

        if end is None:
            end = start + MAX_READ_LINES - 1
        end = min(end, start + MAX_READ_LINES - 1)
        all_lines = text.splitlines()
        if start > len(all_lines):
            return ""
        last = min(end, len(all_lines))

        out: list[str] = []
        used = 0
        for number in range(start, last + 1):
            line = f"{number}\t{all_lines[number - 1]}"
            size = len(line.encode("utf-8")) + 1
            if used + size > MAX_READ_BYTES:
                break
            out.append(line)
            used += size
        # Deliberately no trailing note: every returned line is a numbered
        # source line, and the 400-line / 64 KiB caps are hard. Callers use
        # start=<last+1> to continue.
        return "\n".join(out) + ("\n" if out else "")

    @staticmethod
    def _readable(rel: str) -> bool:
        parts = [part.lower() for part in rel.split("/")]
        for part in parts:
            if part in DENIED_COMPONENTS and part not in ("tests", "test"):
                return False
        name = parts[-1]
        for pattern in DENIED_NAME_GLOBS:
            if fnmatch.fnmatch(name, pattern):
                return False
        if name in LOCKFILE_NAMES:
            return False
        # Tests are readable for context even though the workbench refuses to
        # edit them (the model must be able to see what judges it).
        if rel.startswith("pet-sidecar/tests/"):
            return True
        if not is_core_path_allowed(rel):
            return False
        for pattern in TEST_NAME_GLOBS:
            if fnmatch.fnmatch(name, pattern):
                return False
        return True


__all__ = ["Inspector"]
