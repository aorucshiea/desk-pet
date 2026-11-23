"""On-disk candidate store, lock, and append-only lineage.

Layout (``data_dir``):
  candidates/<id>/candidate.json   full record
  candidates/<id>/diff.patch       unified diff (core or synthetic plugin)
  candidates/<id>/plugin/<name>.py plugin candidate source
  worktrees/<id>/                  detached git worktree (core)
  baselines/<key>/                 cached clean worktree per base commit
  backups/<promotion_id>/          live plugin file before promotion
  lineage.jsonl                    append-only events
  locks/                           single-writer lock
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Iterator

from .models import (
    NotFound,
    WorkbenchError,
    atomic_write_json,
    is_candidate_id,
    is_promotion_id,
    read_json,
)


class FileLock:
    """Cross-platform single-writer lock.

    Uses O_CREAT|O_EXCL because that is atomic on both NTFS and POSIX (unlike
    a "check then create" dance). A lock older than ``stale_after`` is taken
    over: a crashed candidate evaluation must not wedge evolution forever.
    """

    STALE_SECONDS = 15 * 60
    WAIT_SECONDS = 60.0

    def __init__(self, path: Path, *, stale_after: float = STALE_SECONDS,
                 wait: float = WAIT_SECONDS) -> None:
        self.path = Path(path)
        self.stale_after = stale_after
        self.wait = wait
        self._owned = False

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.wait
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age > self.stale_after:
                    # Best effort: another process may win this race; the next
                    # loop iteration sees the new lock and waits normally.
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise WorkbenchError("另一个演化操作正在进行，请稍后再试")
                time.sleep(0.05)
                continue
            else:
                try:
                    os.write(fd, f"pid={os.getpid()}\nts={time.time():.3f}\n".encode("ascii"))
                finally:
                    os.close(fd)
                self._owned = True
                return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._owned:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
            self._owned = False


class EvolveStore:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.candidates_dir = self.data_dir / "candidates"
        self.worktrees_dir = self.data_dir / "worktrees"
        self.baselines_dir = self.data_dir / "baselines"
        self.sandboxes_dir = self.data_dir / "sandboxes"
        self.backups_dir = self.data_dir / "backups"
        self.locks_dir = self.data_dir / "locks"
        self.lineage_path = self.data_dir / "lineage.jsonl"

    def ensure(self) -> None:
        for path in (
            self.data_dir,
            self.candidates_dir,
            self.worktrees_dir,
            self.baselines_dir,
            self.sandboxes_dir,
            self.backups_dir,
            self.locks_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def lock(self) -> FileLock:
        self.ensure()
        # A new object per acquisition: FileLock keeps per-owner state, so a
        # shared instance would let two threads race on `_owned` and leave a
        # spurious lock behind.
        return FileLock(self.locks_dir / "evolve.lock")

    # ── candidates ────────────────────────────────────────────────────
    def candidate_dir(self, candidate_id: str) -> Path:
        if not is_candidate_id(candidate_id):
            raise NotFound(f"找不到候选 {candidate_id!r}")
        return self.candidates_dir / candidate_id

    def candidate_json_path(self, candidate_id: str) -> Path:
        return self.candidate_dir(candidate_id) / "candidate.json"

    def candidate_exists(self, candidate_id: str) -> bool:
        try:
            return self.candidate_json_path(candidate_id).is_file()
        except NotFound:
            return False

    def load_candidate(self, candidate_id: str) -> dict[str, Any]:
        path = self.candidate_json_path(candidate_id)
        if not path.is_file():
            raise NotFound(f"找不到候选 {candidate_id}")
        try:
            record = read_json(path)
        except (OSError, ValueError) as exc:
            raise NotFound(f"候选 {candidate_id} 的记录无法读取: {exc}") from exc
        if not isinstance(record, dict):
            raise NotFound(f"候选 {candidate_id} 的记录格式无效")
        return record

    def save_candidate(self, record: dict[str, Any]) -> None:
        candidate_id = str(record.get("id", ""))
        path = self.candidate_json_path(candidate_id)  # validates the id
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, record)

    def all_candidates(self) -> list[dict[str, Any]]:
        if not self.candidates_dir.is_dir():
            return []
        records: list[dict[str, Any]] = []
        for child in self.candidates_dir.iterdir():
            if not child.is_dir() or not (child / "candidate.json").is_file():
                continue
            try:
                record = read_json(child / "candidate.json")
            except (OSError, ValueError):
                continue
            if isinstance(record, dict) and is_candidate_id(record.get("id")):
                records.append(record)
        records.sort(key=lambda r: (str(r.get("created_at", "")), str(r.get("id", ""))), reverse=True)
        return records

    def list_candidates(self, limit: int = 20) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        return self.all_candidates()[:limit]

    def clear_candidate_dir(self, candidate_id: str) -> None:
        try:
            path = self.candidate_dir(candidate_id)
        except NotFound:
            return
        shutil.rmtree(path, ignore_errors=True)

    # ── lineage ───────────────────────────────────────────────────────
    def append_lineage(self, event: dict[str, Any]) -> None:
        self.ensure()
        line = json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
        # O_APPEND: a learner can tail the file while we write a single event.
        fd = os.open(str(self.lineage_path), os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)

    def lineage_tail(self, limit: int = 5) -> list[dict[str, Any]]:
        if limit <= 0 or not self.lineage_path.is_file():
            return []
        try:
            lines = self.lineage_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                out.append(item)
        return out

    def count_candidates(self) -> int:
        if not self.candidates_dir.is_dir():
            return 0
        return sum(
            1 for child in self.candidates_dir.iterdir()
            if child.is_dir() and (child / "candidate.json").is_file()
        )

    # ── promotions / backups ─────────────────────────────────────────
    def backup_dir(self, promotion_id: str) -> Path:
        if not is_promotion_id(promotion_id):
            raise NotFound(f"找不到晋升记录 {promotion_id!r}")
        return self.backups_dir / promotion_id

    def find_promotion(self, promotion_id: str) -> dict[str, Any]:
        if not is_promotion_id(promotion_id):
            raise NotFound(f"找不到晋升记录 {promotion_id!r}")
        for record in self.all_candidates():
            promotions = record.get("promotions")
            if isinstance(promotions, list):
                for item in promotions:
                    if isinstance(item, dict) and item.get("promotion_id") == promotion_id:
                        return record
            if record.get("promotion_id") == promotion_id:
                return record
        raise NotFound(f"找不到晋升记录 {promotion_id}")

    def sandbox_dir(self, candidate_id: str) -> Path:
        return self.sandboxes_dir / candidate_id


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Small helper kept here so inspector/CLI need not re-implement it."""
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            yield item


__all__ = [
    "EvolveStore",
    "FileLock",
    "iter_jsonl",
]
