"""EvolveService — the single entry point the gateway will call.

All mutating operations take the single-writer lock. Evaluation runs tests
inside a candidate worktree; nothing here writes to the live tree except
``promote`` (plugin-only, backed up, reload-verified) and ``rollback``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from . import lineage
from .evaluator import Evaluator
from .inspector import Inspector
from .models import (
    STATUS_DISCARDED,
    STATUS_EVALUATED,
    STATUS_PROMOTED,
    STATUS_PROPOSED,
    EvolveError,
    NotAllowed,
    NotFound,
    PromotionRefused,
    WorkbenchError,
    atomic_write_text,
    new_candidate_id,
    now_iso,
    sha256_text,
    tail_lines,
)
from .promoter import PluginPromoter
from .store import EvolveStore
from .workbench import (
    Workbench,
    prepare_plugin_candidate,
    validate_core_edits,
)
from ..log_setup import get_logger

log = get_logger()


def _default_data_dir() -> Path:
    env = os.environ.get("PET_EVOLVE_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".pet" / "evolve"


class EvolveService:
    def __init__(
        self,
        repo_root: Path,
        data_dir: Path | None = None,
        plugin_dir: Path | None = None,
        reload_plugins: Callable[[], dict] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).expanduser().resolve()
        self.data_dir = Path(data_dir).expanduser().resolve() if data_dir else _default_data_dir()
        self.plugin_dir = Path(plugin_dir).expanduser().resolve() if plugin_dir else None
        self.reload_plugins = reload_plugins

        self.store = EvolveStore(self.data_dir)
        self.store.ensure()
        self.workbench = Workbench(self.repo_root, self.store.data_dir)
        self.evaluator = Evaluator(self.repo_root, self.store.data_dir)
        self.inspector = Inspector(self.repo_root, self.store, self.plugin_dir)
        self.promoter = PluginPromoter(self.store, self.plugin_dir, reload_plugins)

    # ── read-only surface ────────────────────────────────────────────
    def inspect(self, *, max_files: int = 400) -> str:
        return self.inspector.report(max_files=max_files)

    def read_source(self, path: str, *, start: int = 1, end: int | None = None) -> str:
        return self.inspector.read_source(path, start=start, end=end)

    def list_candidates(self, limit: int = 20) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for record in self.store.list_candidates(limit):
            rows.append(
                {
                    "id": record.get("id"),
                    "kind": record.get("kind"),
                    "title": record.get("title", ""),
                    "goal": record.get("goal", ""),
                    "status": record.get("status"),
                    "eval_status": record.get("eval_status"),
                    "evaluated": bool(record.get("evaluated", False)),
                    "verified": record.get("eval_status") == "verified",
                    "base_commit": record.get("base_commit", ""),
                    "created_at": record.get("created_at", ""),
                    "updated_at": record.get("updated_at", ""),
                    "files_changed": record.get("files_changed", []),
                    "diff_sha256": record.get("diff_sha256", ""),
                    "plugin_name": record.get("plugin_name"),
                    "promotion_id": record.get("promotion_id"),
                }
            )
        return rows

    def show(self, candidate_id: str) -> dict[str, Any]:
        return self.store.load_candidate(candidate_id)

    # ── propose ──────────────────────────────────────────────────────
    def propose(
        self,
        *,
        goal: str,
        plan: str,
        edits: list[dict],
        kind: str = "core",
        title: str = "",
    ) -> dict[str, Any]:
        if kind not in ("core", "plugin"):
            raise NotAllowed(f"未知候选类型: {kind!r}（只支持 core / plugin）")
        if not isinstance(goal, str) or not isinstance(plan, str):
            raise NotAllowed("goal 与 plan 必须是文本")
        if not isinstance(title, str):
            raise NotAllowed("title 必须是文本")
        if not isinstance(edits, list) or not edits:
            raise NotAllowed("edits 不能为空")
        if kind == "plugin" and self.plugin_dir is None:
            raise NotAllowed("未配置插件目录，M1 中无法创建插件候选")

        with self.store.lock():
            candidate_id = new_candidate_id()
            worktree_path = self.store.worktrees_dir / candidate_id
            candidate_dir = self.store.candidate_dir(candidate_id)
            record: dict[str, Any] = {}
            try:
                if kind == "core":
                    normalized_edits = validate_core_edits(edits)
                    state = self.workbench.prepare(candidate_id)
                    files = self.workbench.apply_edits(Path(state["worktree"]), normalized_edits)
                    diff = self.workbench.diff(Path(state["worktree"]), files)
                    plugin_meta: dict[str, Any] = {}
                else:
                    try:
                        state = self.workbench.repo_state()
                    except WorkbenchError:
                        state = {
                            "base_commit": "",
                            "branch": "",
                            "base_dirty": False,
                            "dirty_files": [],
                        }
                    plugin_meta = prepare_plugin_candidate(candidate_dir, self.plugin_dir, edits)
                    normalized_edits = edits
                    files = plugin_meta["files_changed"]
                    diff = plugin_meta["diff"]

                now = now_iso()
                clean_title = (title or "").strip()
                if not clean_title:
                    clean_title = (goal.strip().splitlines() or ["未命名候选"])[0][:80]
                record = {
                    "id": candidate_id,
                    "kind": kind,
                    "title": clean_title,
                    "goal": goal,
                    "plan": plan,
                    "base_commit": state.get("base_commit", ""),
                    "base_dirty": bool(state.get("base_dirty", False)),
                    "dirty_files": list(state.get("dirty_files", [])),
                    "branch": state.get("branch", ""),
                    "status": STATUS_PROPOSED,
                    "eval_status": None,
                    "evaluated": False,
                    "verified": False,
                    "created_at": now,
                    "updated_at": now,
                    "parent_id": None,
                    "edits": normalized_edits,
                    "files_changed": files,
                    "diff_sha256": sha256_text(diff),
                    "evaluations": [],
                    "eval": None,
                    "worktree": state.get("worktree") if kind == "core" else None,
                    "plugin_name": plugin_meta.get("plugin_name"),
                    "plugin_path": plugin_meta.get("plugin_path"),
                    "plugin_newline": plugin_meta.get("plugin_newline", "\n"),
                    "plugin_sha256": (
                        sha256_text(plugin_meta["plugin_text"]) if plugin_meta else None
                    ),
                    "promotions": [],
                    "promotion_id": None,
                }

                candidate_dir.mkdir(parents=True, exist_ok=True)
                atomic_write_text(candidate_dir / "diff.patch", diff)
                self.store.save_candidate(record)
                lineage.append_event(
                    self.store, "proposed", candidate=record, status=STATUS_PROPOSED, actor="pet"
                )
                return record
            except BaseException:
                # No partial candidate: a failed proposal leaves no worktree
                # and no half-written record behind.
                if worktree_path.exists():
                    try:
                        self.workbench.discard(candidate_id, worktree_path)
                    except Exception:
                        log.exception("failed to remove proposal worktree %s", candidate_id)
                if candidate_dir.exists():
                    self.store.clear_candidate_dir(candidate_id)
                raise

    # ── evaluate ─────────────────────────────────────────────────────
    def evaluate(self, candidate_id: str, *, full: bool = False) -> dict[str, Any]:
        with self.store.lock():
            record = self.store.load_candidate(candidate_id)
            if record.get("status") == STATUS_DISCARDED:
                raise NotAllowed("候选已被丢弃，不能评估")
            try:
                result = self.evaluator.evaluate(record, full=full)
            except Exception as exc:  # keep the server alive; the log has the trace
                log.exception("candidate evaluation crashed: %s", candidate_id)
                result = self._internal_error(record, exc, full=full)
            if not isinstance(result, dict):
                result = self._internal_error(
                    record, RuntimeError("evaluator returned a non-dict"), full=full
                )
            result.setdefault("candidate_id", candidate_id)

            evaluations = record.get("evaluations")
            if not isinstance(evaluations, list):
                evaluations = []
            evaluations.append(result)
            record["evaluations"] = evaluations[-20:]
            record["eval"] = result
            record["eval_status"] = result.get("status")
            record["evaluated"] = True
            record["verified"] = result.get("status") == "verified"
            if record.get("status") not in (STATUS_PROMOTED, STATUS_DISCARDED):
                if result.get("status") == "error":
                    # No trustworthy verdict: keep the previous lifecycle state.
                    record["status"] = record.get("status") or STATUS_PROPOSED
                else:
                    record["status"] = STATUS_EVALUATED
            record["updated_at"] = now_iso()
            self.store.save_candidate(record)
            lineage.append_event(
                self.store,
                "evaluated",
                candidate=record,
                status=str(result.get("status", "")),
                actor="pet",
                full=bool(full),
            )
            return result

    def _internal_error(
        self, record: dict[str, Any], exc: Exception, *, full: bool
    ) -> dict[str, Any]:
        from .models import VERDICT_ERROR

        return {
            "candidate_id": record.get("id", ""),
            "kind": record.get("kind", ""),
            "status": VERDICT_ERROR,
            "reason": "internal_error",
            "full": bool(full),
            "base_commit": record.get("base_commit", ""),
            "base_dirty": bool(record.get("base_dirty", False)),
            "dirty_files": record.get("dirty_files", []),
            "tests": [],
            "counts": {"passed": 0, "failed": 0, "errors": 0, "skipped": 0},
            "baseline": None,
            "new_failures": [],
            "duration_ms": 0,
            "log_tail": tail_lines(f"{type(exc).__name__}: {exc}", 20),
            "note": "",
            "finished_at": now_iso(),
        }

    # ── discard / promote / rollback ─────────────────────────────────
    def discard(self, candidate_id: str) -> dict[str, Any]:
        with self.store.lock():
            record = self.store.load_candidate(candidate_id)
            if record.get("status") == STATUS_DISCARDED:
                return record
            if record.get("status") == STATUS_PROMOTED:
                raise PromotionRefused("已上线的插件候选不能直接丢弃，请先 rollback")
            if record.get("kind") == "core":
                self.workbench.discard(candidate_id, record.get("worktree"))
            record["status"] = STATUS_DISCARDED
            record["updated_at"] = now_iso()
            self.store.save_candidate(record)
            lineage.append_event(
                self.store, "discarded", candidate=record, status=STATUS_DISCARDED, actor="pet"
            )
            return record

    def promote(self, candidate_id: str, *, approved_by: str) -> dict[str, Any]:
        with self.store.lock():
            return self.promoter.promote(candidate_id, approved_by=approved_by)

    def rollback(self, promotion_id: str) -> dict[str, Any]:
        with self.store.lock():
            return self.promoter.rollback(promotion_id)


__all__ = [
    "EvolveService",
    "EvolveError",
    "NotAllowed",
    "NotFound",
    "PromotionRefused",
    "WorkbenchError",
]
