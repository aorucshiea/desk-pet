"""Ledger (500 轮台账) — the shared working-memory record of the pet's recent life.

One flat, sequence-numbered list of entries ("多模合一"的上下文载体):

- ``fast``   — one-liners from the 快直觉 lane (kept ≤120 chars, truncated here)
- ``slow``   — fuller records from the 主脑 lane (kept ≤2000 chars, truncated here)
- ``organize`` — the forced-organization rounds (记忆审查 / 一小时总结); NEVER deleted

Every life round (快直觉轮、主脑轮都算 1 轮) calls ``round_tick()``. Every
500 rounds a mark fires: an ``organize`` round becomes due (audit / recap
ALTERNATE per mark) and a fast-entry purge cycle starts — the oldest 50
fast entries go immediately, then up to 100 fast deletions total per cycle
drain progressively across ticks. Independently, once slow entries reach
SLOW_CAP an overflow latch starts deleting 10 slow entries per round until
200 have gone this overflow cycle. ``round_count`` never resets.

Pure stdlib. Store: `<dir>/ledger.json`, written atomically
(same-directory temp file + `os.replace`) on every mutation.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# ─── 台账常数（全部可调） ──────────────────────────────────────────────

CYCLE = 500                    # 每 500 轮触发一次组织轮（记忆审查 / 一小时总结 交替）
PURGE_FAST_AT_MARK = 50        # 组织轮到达瞬间，立刻删掉最旧的 50 条 fast
PURGE_FAST_MAX_PER_CYCLE = 100 # 每个周期最多删 100 条 fast（含 mark 时的 50 条）
SLOW_CAP = 400                 # slow 条数达到 400 → overflow 闩锁吸合
SLOW_PURGE_PER_ROUND = 10      # overflow 期间每轮删 10 条最旧 slow
SLOW_PURGE_MAX_PER_CYCLE = 200 # 每次 overflow 周期最多删 200 条 slow，然后闩锁释放

FAST_TEXT_CAP = 120            # fast 是一句话（超长在这里截断）
SLOW_TEXT_CAP = 2000           # slow 是完整记录（超长在这里截断）

KIND_FAST = "fast"
KIND_SLOW = "slow"
KIND_ORGANIZE = "organize"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clip(text: str, cap: int) -> str:
    text = str(text)
    return text if len(text) <= cap else text[:cap]


def _atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    """Write JSON atomically: same-directory temp file, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class Ledger:
    """500 轮台账 — persistent, sequence-numbered working memory.

    Store file is `<state_dir>/ledger.json`. Every mutation saves
    atomically; callers never need to remember to call save().
    """

    def __init__(self, state_dir: Union[str, Path]) -> None:
        self._dir = Path(state_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "ledger.json"
        self._lock = threading.Lock()
        self._seq = 0
        self._round_count = 0
        self._entries: List[Dict[str, Any]] = []
        # 本周期 fast 剩余可删额度（>0 即 purge_mode 吸合）
        self._purge_fast_left = 0
        # 本次 overflow 周期 slow 剩余可删额度（>0 即 overflow 闩锁吸合）
        self._purge_slow_left = 0
        # recap：组织轮(recap)到达时置位；set_recap() 存正文；consume_recap() 取走并清空
        self._pending_recap = False
        self._recap_text: Optional[str] = None
        self.load()

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Reload state from disk (tolerates a missing / corrupt file)."""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        with self._lock:
            try:
                self._seq = max(0, int(data.get("seq") or 0))
            except (TypeError, ValueError):
                self._seq = 0
            try:
                self._round_count = max(0, int(data.get("round_count") or 0))
            except (TypeError, ValueError):
                self._round_count = 0
            entries = data.get("entries")
            self._entries = [dict(e) for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
            try:
                self._purge_fast_left = max(0, int(data.get("purge_fast_quota") or 0))
            except (TypeError, ValueError):
                self._purge_fast_left = 0
            try:
                self._purge_slow_left = max(0, int(data.get("purge_slow_quota") or 0))
            except (TypeError, ValueError):
                self._purge_slow_left = 0
            self._pending_recap = bool(data.get("pending_recap"))
            self._recap_text = data.get("recap_text") if isinstance(data.get("recap_text"), str) else None

    def save(self) -> None:
        """Persist atomically (tmp file + os.replace)."""
        with self._lock:
            self._write()

    def _write(self) -> None:
        # caller must hold the lock
        _atomic_write_json(self._path, {
            "version": 1,
            "seq": self._seq,
            "round_count": self._round_count,
            "entries": self._entries,
            "purge_fast_quota": self._purge_fast_left,
            "purge_slow_quota": self._purge_slow_left,
            "pending_recap": self._pending_recap,
            "recap_text": self._recap_text,
        })

    # ------------------------------------------------------------------
    # recording
    # ------------------------------------------------------------------

    def _append(self, kind: str, text: str, cap: Optional[int]) -> Dict[str, Any]:
        with self._lock:
            self._seq += 1
            entry = {"seq": self._seq, "kind": kind, "ts": _utc_iso(), "text": _clip(text, cap) if cap else text}
            self._entries.append(entry)
            self._write()
            return dict(entry)

    def record_fast(self, text: str) -> Dict[str, Any]:
        """快直觉一句话（≤120 字符，超长截断）。"""
        return self._append(KIND_FAST, text, FAST_TEXT_CAP)

    def record_slow(self, text: str) -> Dict[str, Any]:
        """主脑完整记录（≤2000 字符，超长截断）。"""
        return self._append(KIND_SLOW, text, SLOW_TEXT_CAP)

    def record_organize(self, text: str) -> Dict[str, Any]:
        """强制组织轮的落账（审查/总结结果；永不删除，不截断）。"""
        return self._append(KIND_ORGANIZE, text, None)

    # ------------------------------------------------------------------
    # round lifecycle
    # ------------------------------------------------------------------

    def round_tick(self, now: Union[None, float, datetime] = None) -> Dict[str, Any]:
        """Call once per life round (快直觉轮、主脑轮都算 1 轮).

        ``round_count`` increments monotonically and NEVER resets. Every
        CYCLE rounds a mark fires. The returned dict is the wiring
        contract for the caller:

        - ``organize_due`` / ``organize_kind`` — run the forced-organization
          round ("audit" = 记忆审查, "recap" = 一小时总结; kinds ALTERNATE
          per mark: 500 audit, 1000 recap, 1500 audit, …).
        - ``purge_fast_now`` — True only on the mark tick (the oldest 50
          fast entries were just deleted immediately).
        - ``purge_mode`` — fast purge quota for this cycle is not yet
          exhausted; keep feeding fast entries, later ticks keep trimming.
        - ``trimmed_fast`` / ``trimmed_slow`` — entries deleted THIS tick.
        - ``overflow`` — slow overflow latch active (caller escalates rest
          reminders and expects the battery to be low).
        """
        with self._lock:
            self._round_count += 1
            trimmed_fast = 0
            trimmed_slow = 0
            organize_due = False
            organize_kind: Optional[str] = None
            purge_fast_now = False

            if self._round_count % CYCLE == 0:
                # ── 组织轮到达（500/1000/1500…，审查与总结交替）──
                organize_due = True
                mark_no = self._round_count // CYCLE
                organize_kind = "audit" if mark_no % 2 == 1 else "recap"
                if organize_kind == "recap":
                    self._pending_recap = True
                # fast 清扫：重置本周期额度，并立刻删掉最旧的 50 条 fast
                self._purge_fast_left = PURGE_FAST_MAX_PER_CYCLE
                purge_fast_now = True
                trimmed_fast = self._trim_oldest(PURGE_FAST_AT_MARK, KIND_FAST)
                self._purge_fast_left -= trimmed_fast
            elif self._purge_fast_left > 0:
                # ── 渐进 fast 清扫：本轮继续删，直到本周期 100 条额度用尽 ──
                trimmed_fast = self._trim_oldest(self._purge_fast_left, KIND_FAST)
                self._purge_fast_left -= trimmed_fast

            # ── slow overflow（独立闩锁）：slow ≥ SLOW_CAP → 吸合，每轮删 10 条 ──
            if self._purge_slow_left <= 0 and self._count_kind(KIND_SLOW) >= SLOW_CAP:
                self._purge_slow_left = SLOW_PURGE_MAX_PER_CYCLE
            if self._purge_slow_left > 0:
                trimmed_slow = self._trim_oldest(min(SLOW_PURGE_PER_ROUND, self._purge_slow_left), KIND_SLOW)
                self._purge_slow_left -= trimmed_slow

            self._write()
            return {
                "round": self._round_count,
                "organize_due": organize_due,
                "organize_kind": organize_kind,
                "purge_fast_now": purge_fast_now,
                "purge_mode": self._purge_fast_left > 0,
                "trimmed_fast": trimmed_fast,
                "overflow": self._purge_slow_left > 0,
                "trimmed_slow": trimmed_slow,
            }

    def _count_kind(self, kind: str) -> int:
        # caller must hold the lock
        return sum(1 for e in self._entries if e.get("kind") == kind)

    def _trim_oldest(self, n: int, kind: str) -> int:
        """Delete up to n oldest entries of `kind`; returns how many were deleted."""
        # caller must hold the lock
        if n <= 0:
            return 0
        kept: List[Dict[str, Any]] = []
        removed = 0
        for e in self._entries:
            if removed < n and e.get("kind") == kind:
                removed += 1
                continue
            kept.append(e)
        self._entries = kept
        return removed

    # ------------------------------------------------------------------
    # recap（一小时总结：附加到下一条真实用户消息）
    # ------------------------------------------------------------------

    def set_recap(self, text: str) -> None:
        """Caller stores the main model's recap summary text."""
        with self._lock:
            self._recap_text = str(text)
            self._pending_recap = True
            self._write()

    def consume_recap(self) -> Optional[str]:
        """Return the stored recap text (or None) and clear the pending slot."""
        with self._lock:
            text = self._recap_text
            self._recap_text = None
            self._pending_recap = False
            self._write()
            return text

    @property
    def pending_recap(self) -> bool:
        with self._lock:
            return self._pending_recap

    # ------------------------------------------------------------------
    # accessors
    # ------------------------------------------------------------------

    def entries(self) -> List[Dict[str, Any]]:
        """Copies of all entries, oldest first."""
        with self._lock:
            return [dict(e) for e in self._entries]

    def counts(self) -> Dict[str, int]:
        with self._lock:
            return {
                KIND_FAST: self._count_kind(KIND_FAST),
                KIND_SLOW: self._count_kind(KIND_SLOW),
                KIND_ORGANIZE: self._count_kind(KIND_ORGANIZE),
            }

    def round_count(self) -> int:
        """Total life rounds ever (never resets, not even after a mark or death)."""
        return self._round_count
