"""500 轮台账 (ledger) tests — round lifecycle, organize marks, purges, recap.

Plain pytest with tmp_path for the store dir, matching the existing test
style. Constants are imported from gateway/ledger.py so the tests break
loudly if someone retunes the tunables.
"""

from __future__ import annotations

import json
from pathlib import Path

from gateway.ledger import (
    CYCLE,
    PURGE_FAST_AT_MARK,
    PURGE_FAST_MAX_PER_CYCLE,
    SLOW_CAP,
    SLOW_PURGE_MAX_PER_CYCLE,
    SLOW_PURGE_PER_ROUND,
    Ledger,
)


def _mk(tmp_path: Path, name: str = "state") -> Ledger:
    return Ledger(tmp_path / name)


def _feed_fast(l: Ledger, n: int, tag: str = "f") -> None:
    for i in range(n):
        l.record_fast(f"{tag}{i}")


def _feed_slow(l: Ledger, n: int, tag: str = "s") -> None:
    for i in range(n):
        l.record_slow(f"{tag}{i}")


# ─── constants sanity ──────────────────────────────────────────────────

def test_constants_table():
    assert CYCLE == 500
    assert PURGE_FAST_AT_MARK == 50
    assert PURGE_FAST_MAX_PER_CYCLE == 100
    assert SLOW_CAP == 400
    assert SLOW_PURGE_PER_ROUND == 10
    assert SLOW_PURGE_MAX_PER_CYCLE == 200


# ─── recording & seq ───────────────────────────────────────────────────

def test_seq_is_monotonic_across_kinds(tmp_path):
    l = _mk(tmp_path)
    a = l.record_fast("a")
    b = l.record_slow("b")
    c = l.record_organize("c")
    d = l.record_fast("d")
    assert [a["seq"], b["seq"], c["seq"], d["seq"]] == [1, 2, 3, 4]
    assert a["kind"] == "fast" and b["kind"] == "slow" and c["kind"] == "organize"
    assert all(e["ts"] for e in (a, b, c, d))


def test_text_truncation_caps(tmp_path):
    l = _mk(tmp_path)
    f = l.record_fast("x" * 500)
    s = l.record_slow("y" * 9999)
    o = l.record_organize("z" * 9999)
    assert len(f["text"]) == 120
    assert len(s["text"]) == 2000
    assert len(o["text"]) == 9999  # organize is never truncated
    # boundary: exactly at cap stays whole
    assert len(l.record_fast("q" * 120)["text"]) == 120
    assert len(l.record_fast("q" * 119)["text"]) == 119


# ─── round_tick basics ─────────────────────────────────────────────────

def test_round_tick_counts_every_call(tmp_path):
    l = _mk(tmp_path)
    for _ in range(37):
        r = l.round_tick()
    assert l.round_count() == 37
    assert r["round"] == 37
    assert r["organize_due"] is False


def test_round_count_never_resets_after_marks(tmp_path):
    l = _mk(tmp_path)
    for _ in range(CYCLE * 2 + 5):
        l.round_tick()
    assert l.round_count() == CYCLE * 2 + 5


# ─── organize marks alternate audit/recap ──────────────────────────────

def test_organize_marks_alternate_audit_recap(tmp_path):
    l = _mk(tmp_path)
    kinds = []
    for _ in range(CYCLE * 3):
        r = l.round_tick()
        if r["organize_due"]:
            kinds.append(r["organize_kind"])
    assert kinds == ["audit", "recap", "audit"]
    assert kinds[0] == "audit"   # 500  → 记忆审查
    assert kinds[1] == "recap"   # 1000 → 一小时总结
    assert kinds[2] == "audit"   # 1500 → 记忆审查


# ─── fast purge at the mark ────────────────────────────────────────────

def test_mark_purges_oldest_50_fast_immediately(tmp_path):
    l = _mk(tmp_path)
    _feed_fast(l, 120, "old")           # seq 1..120
    _feed_slow(l, 5, "slow")
    l.record_organize("审查记录")
    for _ in range(CYCLE - 1):
        l.round_tick()
    r = l.round_tick()                  # tick 500 → mark
    assert r["organize_due"] is True
    assert r["purge_fast_now"] is True
    assert r["trimmed_fast"] == 50
    seqs = [e["seq"] for e in l.entries() if e["kind"] == "fast"]
    assert seqs == list(range(51, 121))          # oldest 50 fast gone
    assert l.counts() == {"fast": 70, "slow": 5, "organize": 1}
    # purge cycle is active: 50 of the 100 quota remains
    assert r["purge_mode"] is True


def test_slow_and_organize_untouched_by_fast_purge(tmp_path):
    l = _mk(tmp_path)
    _feed_slow(l, 10, "keep")
    l.record_organize("never-delete")
    _feed_fast(l, 60)
    for _ in range(CYCLE):
        l.round_tick()
    assert l.counts()["slow"] == 10
    assert l.counts()["organize"] == 1


# ─── progressive purge across ticks ────────────────────────────────────

def test_progressive_purge_continues_until_100_then_stops(tmp_path):
    l = _mk(tmp_path)
    _feed_fast(l, 300, "old")                     # seq 1..300
    for _ in range(CYCLE - 1):
        l.round_tick()
    r = l.round_tick()                            # tick 500 → mark: oldest 50 gone at once
    assert r["organize_due"] is True
    assert r["purge_fast_now"] is True
    assert r["trimmed_fast"] == 50
    assert [e["seq"] for e in l.entries() if e["kind"] == "fast"] == list(range(51, 301))
    # quota remains (100 − 50); next tick drains the REST of the old fast
    # entries in one go (spec: "as many as remain needed")
    r2 = l.round_tick()
    assert r2["purge_mode"] is False              # quota exhausted this tick
    assert r2["trimmed_fast"] == 50               # 50+50 = 100 total this cycle
    assert [e["seq"] for e in l.entries() if e["kind"] == "fast"] == list(range(101, 301))
    # with nothing old left, later ticks keep quota idle but trim nothing
    r3 = l.round_tick()
    assert r3["purge_mode"] is False
    assert r3["trimmed_fast"] == 0
    # …and the NEXT mark (tick 1000) resets the cycle and purges again.
    for _ in range(CYCLE - 3):
        l.round_tick()
    r4 = l.round_tick()
    assert l.round_count() == CYCLE * 2
    assert r4["organize_due"] is True and r4["purge_fast_now"] is True
    assert r4["trimmed_fast"] == 50


def test_purge_quota_capped_at_100_per_cycle(tmp_path):
    l = _mk(tmp_path)
    _feed_fast(l, 400, "old")
    total = 0
    seen_mark = False
    for _ in range(CYCLE + 20):
        r = l.round_tick()
        total += r["trimmed_fast"]
        if r["purge_fast_now"]:
            seen_mark = True
        if seen_mark and not r["purge_mode"]:
            break
    assert seen_mark
    assert total == PURGE_FAST_MAX_PER_CYCLE
    assert l.counts()["fast"] == 400 - 100


def test_progressive_purge_spreads_over_fed_entries(tmp_path):
    """Feed fast entries between mark ticks: the purge drains oldest first."""
    l = _mk(tmp_path)
    for _ in range(CYCLE - 1):
        l.round_tick()
        l.record_fast(f"f{l.round_count()}")      # a fast entry per round
    r = l.round_tick()                            # mark: oldest 50 gone
    assert r["purge_fast_now"] is True and r["trimmed_fast"] == 50
    assert r["purge_mode"] is True                # 50 quota left
    # feed nothing yet: next tick drains as many old fast entries as remain
    # needed (still within the 100-per-cycle cap)
    r2 = l.round_tick()
    assert r2["trimmed_fast"] == 50
    assert r2["purge_mode"] is False              # 100 deleted → cycle over
    # feed new entries; they are NOT trimmed (quota exhausted)
    for _ in range(20):
        l.record_fast("new")
    r3 = l.round_tick()
    assert r3["trimmed_fast"] == 0
    assert r3["purge_mode"] is False
    assert l.counts()["fast"] == 399 + 20   # 499 fed − 100 purged + 20 new


def test_purge_stops_when_no_fast_entries_left(tmp_path):
    l = _mk(tmp_path)
    _feed_fast(l, 10)
    for _ in range(CYCLE - 1):
        l.round_tick()
    r = l.round_tick()
    assert r["trimmed_fast"] == 10      # only 10 exist
    assert r["purge_mode"] is True      # quota remains, but nothing to trim
    r2 = l.round_tick()
    assert r2["trimmed_fast"] == 0
    assert r2["purge_mode"] is True


# ─── slow overflow ─────────────────────────────────────────────────────

def test_slow_overflow_latch_purges_10_per_round_until_200(tmp_path):
    l = _mk(tmp_path)
    _feed_slow(l, SLOW_CAP - 1)          # 399 → no overflow yet
    r = l.round_tick()
    assert r["overflow"] is False
    assert r["trimmed_slow"] == 0
    l.record_slow("第400条")             # hits the cap
    r = l.round_tick()
    assert r["overflow"] is True
    assert r["trimmed_slow"] == SLOW_PURGE_PER_ROUND
    total = r["trimmed_slow"]
    guard = 0
    while total < SLOW_PURGE_MAX_PER_CYCLE and guard < 100:
        r = l.round_tick()
        total += r["trimmed_slow"]
        guard += 1
    assert total == SLOW_PURGE_MAX_PER_CYCLE
    # latch released: no more slow trimming…
    r = l.round_tick()
    assert r["overflow"] is False
    assert r["trimmed_slow"] == 0
    # …until the count climbs back to the cap.
    _feed_slow(l, SLOW_CAP)
    r = l.round_tick()
    assert r["overflow"] is True
    assert r["trimmed_slow"] == SLOW_PURGE_PER_ROUND


def test_slow_overflow_skips_ticks_below_cap(tmp_path):
    l = _mk(tmp_path)
    _feed_slow(l, 50)
    for _ in range(5):
        r = l.round_tick()
    assert r["overflow"] is False
    assert l.counts()["slow"] == 50


def test_organize_entries_survive_slow_overflow(tmp_path):
    l = _mk(tmp_path)
    _feed_slow(l, SLOW_CAP + 10)
    l.record_organize("重要组织记录")
    for _ in range(30):
        l.round_tick()
    assert l.counts()["organize"] == 1
    assert l.entries()[-1]["text"] == "重要组织记录"


# ─── recap flow ────────────────────────────────────────────────────────

def test_pending_recap_set_on_recap_mark(tmp_path):
    l = _mk(tmp_path)
    for _ in range(CYCLE * 2):
        r = l.round_tick()
    assert r["organize_kind"] == "recap"
    assert l.pending_recap is True
    l.set_recap("凌凌这一个小时的总结。")
    assert l.pending_recap is True
    text = l.consume_recap()
    assert text == "凌凌这一个小时的总结。"
    assert l.pending_recap is False
    assert l.consume_recap() is None


def test_audit_mark_does_not_set_recap(tmp_path):
    l = _mk(tmp_path)
    for _ in range(CYCLE):
        r = l.round_tick()
    assert r["organize_kind"] == "audit"
    assert l.pending_recap is False


# ─── persistence ───────────────────────────────────────────────────────

def test_atomic_persistence_roundtrip(tmp_path):
    l = _mk(tmp_path)
    _feed_fast(l, 3)
    _feed_slow(l, 2)
    l.record_organize("o")
    for _ in range(3):
        l.round_tick()
    raw = json.loads((tmp_path / "state" / "ledger.json").read_text("utf-8"))
    assert raw["seq"] == 6
    assert raw["round_count"] == 3
    assert len(raw["entries"]) == 6

    l2 = _mk(tmp_path)  # reload from the same dir
    assert l2.round_count() == 3
    assert l2.counts() == {"fast": 3, "slow": 2, "organize": 1}
    assert [e["seq"] for e in l2.entries()] == [1, 2, 3, 4, 5, 6]
    # seq continues monotonically after reload
    assert l2.record_fast("继续")["seq"] == 7


def test_persistence_of_purge_and_recap_state(tmp_path):
    l = _mk(tmp_path)
    _feed_fast(l, 200)
    for _ in range(CYCLE):
        l.round_tick()
    fast_after_mark = l.counts()["fast"]       # 200 − 50 = 150
    # mid-purge state (the leftover quota) survives a reload
    l2 = _mk(tmp_path)
    r = l2.round_tick()
    assert r["trimmed_fast"] == 50             # quota came back from disk…
    assert r["purge_mode"] is False            # …and is now exhausted (100/cycle)
    assert l2.counts()["fast"] == fast_after_mark - 50


def test_ledger_tolerates_missing_and_corrupt(tmp_path):
    l = _mk(tmp_path)                    # no file yet
    assert l.round_count() == 0
    p = tmp_path / "state" / "ledger.json"
    p.write_text("{oops", encoding="utf-8")
    l2 = _mk(tmp_path)
    assert l2.round_count() == 0
    assert l2.entries() == []


def test_entry_shape_contract(tmp_path):
    l = _mk(tmp_path)
    e = l.record_fast("hi")
    assert set(e.keys()) == {"seq", "kind", "ts", "text"}
