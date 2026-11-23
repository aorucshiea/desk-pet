"""Candidate evaluation: compile + focused tests, baseline comparison,
and a subprocess smoke test for plugins.

A verdict is only ``verified`` when the candidate itself passes *and* the
same tests pass at its base commit with no failures the baseline did not
already have. A red baseline is information, not permission to call a red
candidate green.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any

from .models import (
    VERDICT_ERROR,
    VERDICT_FAILED,
    VERDICT_UNVERIFIED,
    VERDICT_VERIFIED,
    WorkbenchError,
    now_iso,
    tail_lines,
)
from .runner import run_command
from .workbench import Workbench, plugin_candidate_path, read_source_text
from .. import plugin_policy

FOCUSED_TIMEOUT = 300.0
FULL_TIMEOUT = 900.0
PLUGIN_TIMEOUT = 60.0
MAX_FOCUSED_TESTS = 8
LOG_TAIL_LINES = 80

_COUNT_PATTERNS = {
    "passed": re.compile(r"(\d+)\s+passed\b"),
    "failed": re.compile(r"(\d+)\s+failed\b"),
    "errors": re.compile(r"(\d+)\s+error(?:s)?\b"),
    "skipped": re.compile(r"(\d+)\s+skipped\b"),
}
_FAILED_ID_RE = re.compile(r"^\s*(?:FAILED|ERROR)\s+(\S+)", re.MULTILINE)

# The harness uses a real PluginContext + a dummy kernel so a plugin's
# registrations behave exactly as they would at load time, without starting
# the gateway or importing the plugin into the evaluator process.
_SMOKE_SCRIPT = r'''
import json, sys, traceback
plugin_path, pkg_root = sys.argv[1], sys.argv[2]
sys.path.insert(0, pkg_root)
result = {"ok": False, "stage": "start", "tools": []}
try:
    from gateway import plugin_policy
    from gateway.petplugins import PluginContext, PluginManager, resolve_config
    with open(plugin_path, "r", encoding="utf-8") as fh:
        source = fh.read()
    violations = plugin_policy.check_source(source)
    if violations:
        result = {"ok": False, "stage": "policy", "tools": [], "violations": violations}
    else:
        try:
            tree = compile(source, plugin_path, "exec")
        except SyntaxError as exc:
            result = {"ok": False, "stage": "syntax", "tools": [], "error": "%s (line %s)" % (exc.msg, exc.lineno)}
        else:
            module = type(sys)("evolve_candidate")
            module.__file__ = plugin_path
            exec(tree, module.__dict__)
            apply = getattr(module, "apply", None)
            if not callable(apply):
                result = {"ok": False, "stage": "missing_apply", "tools": [], "error": "plugin has no apply(ctx)"}
            else:
                manager = PluginManager({}, seed_organs=False)
                ctx = PluginContext("evolve_candidate", manager)
                schema = getattr(module, "config", None)
                if isinstance(schema, dict):
                    ctx.config = resolve_config(schema, {}, "evolve_candidate")
                apply(ctx)
                tools = sorted(ctx._tools.keys())
                if tools:
                    result = {"ok": True, "stage": "loaded", "tools": tools}
                else:
                    result = {"ok": False, "stage": "no_tools", "tools": [], "error": "apply(ctx) registered no tools"}
except BaseException as exc:
    result = {
        "ok": False,
        "stage": "error",
        "tools": [],
        "error": repr(exc),
        "traceback": traceback.format_exc()[-4000:],
    }
print("EVOLVE_SMOKE:" + json.dumps(result, ensure_ascii=False))
'''


def _zero_counts() -> dict[str, int]:
    return {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}


def _parse_counts(output: str) -> dict[str, int]:
    counts = _zero_counts()
    for key, pattern in _COUNT_PATTERNS.items():
        matches = pattern.findall(output or "")
        if matches:
            counts[key] = int(matches[-1])
    return counts


def _failed_ids(output: str) -> list[str]:
    return sorted({match.strip() for match in _FAILED_ID_RE.findall(output or "")})


def _combine_logs(*chunks: str) -> str:
    return tail_lines("\n".join(chunk for chunk in chunks if chunk), LOG_TAIL_LINES)


class Evaluator:
    def __init__(self, repo_root: Path, data_dir: Path) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.data_dir = Path(data_dir).resolve()
        self.workbench = Workbench(self.repo_root, self.data_dir)

    # ── public entry ─────────────────────────────────────────────────
    def evaluate(self, candidate: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
        started = time.monotonic()
        if candidate.get("kind") == "plugin":
            return self._evaluate_plugin(candidate, full=full, started=started)
        return self._evaluate_core(candidate, full=full, started=started)

    # ── shared result plumbing ───────────────────────────────────────
    def _base_result(
        self,
        candidate: dict[str, Any],
        *,
        full: bool,
        started: float,
        status: str,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "candidate_id": candidate.get("id", ""),
            "kind": candidate.get("kind", ""),
            "status": status,
            "reason": reason,
            "full": bool(full),
            "base_commit": candidate.get("base_commit", ""),
            "base_dirty": bool(candidate.get("base_dirty", False)),
            "dirty_files": candidate.get("dirty_files", []),
            "tests": [],
            "counts": _zero_counts(),
            "baseline": None,
            "new_failures": [],
            "duration_ms": int((time.monotonic() - started) * 1000),
            "log_tail": "",
            "python": "",
            "sandbox": "",
            "note": self._dirty_note(candidate),
            "finished_at": now_iso(),
        }

    @staticmethod
    def _dirty_note(candidate: dict[str, Any]) -> str:
        dirty = candidate.get("dirty_files") or []
        if not dirty:
            return ""
        return (
            f"live 工作树有 {len(dirty)} 个未提交改动；候选只基于 {str(candidate.get('base_commit', ''))[:12]}，"
            "评估在独立 worktree 中进行。"
        )

    def _python(self) -> str:
        candidates = (
            self.repo_root / "pet-sidecar" / ".venv" / "Scripts" / "python.exe",
            self.repo_root / "pet-sidecar" / ".venv" / "bin" / "python",
        )
        for path in candidates:
            if path.is_file():
                return str(path)
        return sys.executable

    def _fresh_sandbox(self, label: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", label)[:120]
        path = self.data_dir / "sandboxes" / safe
        shutil.rmtree(path, ignore_errors=True)
        (path / "home").mkdir(parents=True, exist_ok=True)
        (path / "memories").mkdir(parents=True, exist_ok=True)
        (path / "logs").mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _env(sandbox: Path) -> dict[str, str]:
        return {
            "PET_HOME": str(sandbox),
            "PET_MEMORY_DIR": str(sandbox / "memories"),
            "PET_LOG_DIR": str(sandbox / "logs"),
            "PYTHONPYCACHEPREFIX": str(sandbox / "pycache"),
        }

    # ── core ─────────────────────────────────────────────────────────
    def _evaluate_core(
        self, candidate: dict[str, Any], *, full: bool, started: float
    ) -> dict[str, Any]:
        files = [str(f) for f in (candidate.get("files_changed") or [])]
        changed_py = [f for f in files if f.lower().endswith(".py")]
        result = self._base_result(
            candidate, full=full, started=started, status=VERDICT_UNVERIFIED, reason=""
        )

        if not files:
            result["reason"] = "no_files_changed"
            return result
        non_python = [f for f in files if not f.lower().endswith(".py")]
        if non_python:
            # The Python suite cannot prove JS/CSS/HTML/docs behaviour.
            result["reason"] = "manual_review_required"
            result["log_tail"] = "非 Python 改动，需要人工审查: " + ", ".join(non_python[:12])
            return result

        worktree_raw = candidate.get("worktree") or ""
        worktree = Path(str(worktree_raw)) if worktree_raw else None
        if worktree is None or not worktree.is_dir():
            result.update(status=VERDICT_ERROR, reason="candidate_worktree_missing")
            return result

        tests = self._focused_tests(worktree, changed_py)
        if not full and not tests:
            result["reason"] = "no_focused_tests"
            result["log_tail"] = "没有找到与改动文件对应的测试。"
            return result

        python = self._python()
        result["python"] = python
        sandbox = self._fresh_sandbox(str(candidate.get("id", "candidate")))
        result["sandbox"] = str(sandbox)

        compile_targets = self._compile_targets(worktree, files)
        compile_run = run_command(
            [python, "-m", "compileall", "-q", *compile_targets],
            cwd=worktree,
            timeout=FOCUSED_TIMEOUT,
            env=self._env(sandbox),
            sandbox=sandbox,
        )
        if not compile_run["ok"]:
            result.update(
                status=VERDICT_FAILED,
                reason="compile_error",
                log_tail=_combine_logs(compile_run.get("output", "")),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            result["compile"] = {
                "returncode": compile_run.get("returncode"),
                "duration_ms": compile_run.get("duration_ms"),
            }
            return result

        candidate_run = self._run_pytest(
            python, worktree, tests, full=full, sandbox=sandbox
        )
        result["tests"] = ["(full suite)"] if full else tests
        result["counts"] = _parse_counts(candidate_run.get("output", ""))
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
        result["compile"] = {
            "returncode": compile_run.get("returncode"),
            "duration_ms": compile_run.get("duration_ms"),
        }

        # Baseline must be compared against the same test set at the same
        # commit. A cached worktree keeps this from doubling git cost.
        base_commit = str(candidate.get("base_commit") or "")
        try:
            baseline_wt = self.workbench.baseline_worktree(base_commit)
            baseline_sandbox = self._fresh_sandbox(f"_baseline_{base_commit[:12]}")
            baseline_run = self._run_pytest(
                python, baseline_wt, tests, full=full, sandbox=baseline_sandbox
            )
        except WorkbenchError as exc:
            result.update(status=VERDICT_ERROR, reason=f"baseline_unavailable: {exc}")
            result["log_tail"] = _combine_logs(
                candidate_run.get("output", ""), str(exc)
            )
            return result

        base_counts = _parse_counts(baseline_run.get("output", ""))
        base_bad = set(_failed_ids(baseline_run.get("output", "")))
        baseline_ok = (
            baseline_run["returncode"] == 0
            and base_counts["failed"] == 0
            and base_counts["errors"] == 0
        )
        if baseline_run["timed_out"]:
            baseline_status = "timeout"
        elif baseline_ok:
            baseline_status = "passed"
        elif baseline_run["returncode"] == 0 or (
            base_counts["failed"] == 0
            and base_counts["errors"] == 0
            and baseline_run["returncode"] != 0
        ):
            baseline_status = "unusable"
        else:
            baseline_status = "failed"
        result["baseline"] = {
            "status": baseline_status,
            "returncode": baseline_run.get("returncode"),
            "counts": base_counts,
            "failed_tests": sorted(base_bad),
            "duration_ms": baseline_run.get("duration_ms"),
            "timed_out": bool(baseline_run.get("timed_out")),
        }

        cand_counts = result["counts"]
        cand_bad = set(_failed_ids(candidate_run.get("output", "")))
        new_failures = sorted(cand_bad - base_bad)
        if (
            not new_failures
            and not cand_bad
            and not base_bad
            and cand_counts["failed"] > base_counts["failed"]
        ):
            new_failures = [
                f"~{cand_counts['failed'] - base_counts['failed']} new failure(s)"
            ]
        result["new_failures"] = new_failures
        result["log_tail"] = _combine_logs(candidate_run.get("output", ""))

        if baseline_status == "unusable":
            result.update(status=VERDICT_ERROR, reason="baseline_unusable")
            return result
        if baseline_status == "timeout":
            result.update(status=VERDICT_ERROR, reason="baseline_timeout")
            return result
        if candidate_run["timed_out"]:
            result.update(status=VERDICT_FAILED, reason="timeout")
            return result
        if candidate_run["returncode"] != 0 and sum(cand_counts.values()) == 0:
            result.update(status=VERDICT_ERROR, reason="test_infrastructure")
            return result

        if (
            candidate_run["returncode"] == 0
            and cand_counts["failed"] == 0
            and cand_counts["errors"] == 0
            and not new_failures
        ):
            if baseline_status == "failed":
                # A green candidate on a red baseline is not a trustworthy
                # verification: the suite is already broken at its base.
                result.update(status=VERDICT_UNVERIFIED, reason="baseline_failed")
            else:
                result.update(status=VERDICT_VERIFIED, reason="")
        else:
            result.update(status=VERDICT_FAILED, reason="tests_failed")
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
        result["finished_at"] = now_iso()
        return result

    def _run_pytest(
        self,
        python: str,
        worktree: Path,
        tests: list[str],
        *,
        full: bool,
        sandbox: Path,
    ) -> dict[str, Any]:
        cmd = [python, "-m", "pytest", "-q"]
        if not full:
            cmd.extend(tests)
        return run_command(
            cmd,
            cwd=Path(worktree) / "pet-sidecar",
            timeout=FULL_TIMEOUT if full else FOCUSED_TIMEOUT,
            env=self._env(sandbox),
            sandbox=sandbox,
        )

    def _compile_targets(self, worktree: Path, files: list[str]) -> list[str]:
        targets: list[str] = []
        seen_dirs: set[str] = set()
        for rel in files:
            if not rel.lower().endswith(".py"):
                continue
            targets.append(str(Path(worktree) / rel))
            parent = PurePosixPath(rel).parent
            while parent.as_posix() not in (".", "pet-sidecar", ""):
                candidate_dir = Path(worktree) / parent
                key = str(candidate_dir)
                if key not in seen_dirs and (candidate_dir / "__init__.py").is_file():
                    seen_dirs.add(key)
                    targets.append(key)
                parent = parent.parent
        return targets

    # ── focused test mapping ─────────────────────────────────────────
    def _focused_tests(self, worktree: Path, changed_py: list[str]) -> list[str]:
        tests_dir = Path(worktree) / "pet-sidecar" / "tests"
        if not tests_dir.is_dir():
            return []
        available = sorted(
            path.name for path in tests_dir.iterdir()
            if path.is_file() and path.suffix == ".py"
        )
        chosen: dict[str, int] = {}

        def add(name: str, priority: int) -> None:
            if name in available:
                chosen[name] = min(priority, chosen.get(name, priority))

        for rel in changed_py:
            if not rel.startswith("pet-sidecar/"):
                continue
            stem = Path(rel).stem
            if rel == "pet-sidecar/gateway/server.py":
                for name in available:
                    if name.startswith("test_gateway_"):
                        add(name, 0)
                continue
            if rel.startswith("pet-sidecar/gateway/"):
                add(f"test_gateway_{stem}.py", 0)
                add(f"test_{stem}.py", 0)
                for name in available:
                    if stem and stem in name:
                        add(name, 1)
            elif rel.startswith("pet-sidecar/deepseek_web/"):
                for name in available:
                    if name.startswith(f"test_{stem}"):
                        add(name, 0)
                    elif stem and stem in name:
                        add(name, 1)
            elif rel.startswith("pet-sidecar/laya_sidecar/"):
                for name in available:
                    if name.startswith(f"test_{stem}"):
                        add(name, 0)
                    elif stem and stem in name:
                        add(name, 1)

        ordered = sorted(chosen.items(), key=lambda item: (item[1], item[0]))
        return [f"tests/{name}" for name, _ in ordered[:MAX_FOCUSED_TESTS]]

    # ── plugin ───────────────────────────────────────────────────────
    def _evaluate_plugin(
        self, candidate: dict[str, Any], *, full: bool, started: float
    ) -> dict[str, Any]:
        result = self._base_result(
            candidate, full=full, started=started, status=VERDICT_UNVERIFIED, reason=""
        )
        candidate_id = str(candidate.get("id", ""))
        name = str(candidate.get("plugin_name") or "")
        try:
            path = plugin_candidate_path(
                self.data_dir / "candidates" / candidate_id, name
            )
        except Exception:
            result.update(status=VERDICT_FAILED, reason="invalid_plugin_name")
            return result
        if not path.is_file():
            result.update(status=VERDICT_ERROR, reason="plugin_candidate_missing")
            return result

        try:
            source, _ = read_source_text(path)
        except Exception as exc:
            result.update(status=VERDICT_FAILED, reason="plugin_source_unreadable")
            result["log_tail"] = str(exc)
            return result

        violations = plugin_policy.check_source(source)
        if violations:
            result.update(status=VERDICT_FAILED, reason="policy_violation")
            result["log_tail"] = plugin_policy.format_violations(violations)
            return result
        try:
            ast.parse(source)
        except SyntaxError as exc:
            result.update(status=VERDICT_FAILED, reason="syntax_error")
            result["log_tail"] = f"line {exc.lineno}: {exc.msg}"
            return result

        python = self._python()
        result["python"] = python
        sandbox = self._fresh_sandbox(candidate_id or "plugin")
        result["sandbox"] = str(sandbox)
        package_root = self._plugin_package_root()
        run = run_command(
            [python, "-c", _SMOKE_SCRIPT, str(path), str(package_root)],
            cwd=package_root,
            timeout=PLUGIN_TIMEOUT,
            env=self._env(sandbox),
            sandbox=sandbox,
        )
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
        result["log_tail"] = tail_lines(run.get("output", ""), LOG_TAIL_LINES)

        payload = self._parse_smoke(run.get("output", ""))
        if run.get("timed_out"):
            result.update(status=VERDICT_FAILED, reason="smoke_timeout")
            return result
        if payload is None:
            if run.get("returncode") == 127:
                result.update(status=VERDICT_ERROR, reason="python_unavailable")
            else:
                result.update(status=VERDICT_FAILED, reason="smoke_harness_failed")
            return result

        result["smoke"] = {
            "stage": payload.get("stage"),
            "tools": payload.get("tools") or [],
            "error": payload.get("error") or "",
        }
        if not payload.get("ok"):
            reason = str(payload.get("stage") or "smoke_failed")
            result.update(status=VERDICT_FAILED, reason=reason)
            return result
        if not payload.get("tools"):
            result.update(status=VERDICT_FAILED, reason="no_tools_registered")
            return result
        result.update(status=VERDICT_VERIFIED, reason="")
        result["finished_at"] = now_iso()
        return result

    def _plugin_package_root(self) -> Path:
        """Where the harness imports ``gateway`` from.

        Production candidates run against the repo's own kernel. A synthetic
        test repo may not contain ``gateway/petplugins.py``; in that case the
        evaluator's installed package is the only kernel available, and it is
        the code the plugin would actually load under here.
        """
        repo_pkg = self.repo_root / "pet-sidecar"
        if (repo_pkg / "gateway" / "petplugins.py").is_file():
            return repo_pkg
        installed_pkg = Path(__file__).resolve().parents[2]
        if (installed_pkg / "gateway" / "petplugins.py").is_file():
            return installed_pkg
        return repo_pkg

    @staticmethod
    def _parse_smoke(output: str) -> dict[str, Any] | None:
        marker = "EVOLVE_SMOKE:"
        for line in reversed((output or "").splitlines()):
            if line.startswith(marker):
                try:
                    payload = json.loads(line[len(marker):])
                except ValueError:
                    return None
                return payload if isinstance(payload, dict) else None
        return None


__all__ = ["Evaluator"]
