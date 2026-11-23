"""生命体征 (vitals) tests — 电量 + 血量 + 睡眠状态机.

Plain pytest with tmp_path for the store dir, matching the existing test
style. All exact numeric expectations come straight from the constants at
the top of gateway/vitals.py.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from gateway.vitals import (
    BATTERY_RECOVER_PERCENT,
    BATTERY_WARN_PERCENT,
    CHARGE_ASLEEP_PER_MIN,
    CHARGE_AWAKE_PER_MIN,
    CONTEXT_LIMIT,
    DRAIN_SMALL_FACTOR,
    DRAIN_THINKING_FACTOR,
    FORCE_SLEEP_S,
    SELF_SLEEP_MIN_S,
    SLEEP_MULT_BASE,
    SLEEP_MULT_MAX,
    SLEEP_MULT_MIN,
    SOMATIC_TEXTS,
    Vitals,
)

T0 = datetime(2026, 11, 23, 12, 0, 0, tzinfo=timezone.utc)


def _s(n: float) -> float:
    """T0 + n seconds, as an epoch float."""
    return (T0 + timedelta(seconds=n)).timestamp()


def _mk(tmp_path: Path, **kw) -> Vitals:
    return Vitals(tmp_path / "state", **kw)


def _fake_epoch(monkeypatch, base: float = None) -> None:
    """Pin time.time() so sleep timers are deterministic (no wall clock).

    Default base == T0's epoch, so `started_at` recorded by start_sleep()
    lines up with the `_s()` timestamps passed to wake/natural_wake.
    """
    import gateway.vitals as vitals_mod

    monkeypatch.setattr(vitals_mod.time, "time", lambda: T0.timestamp() if base is None else base)


# ─── 电量 (battery) ────────────────────────────────────────────────────

def test_capacity_defaults_to_2x_context_limit(tmp_path):
    v = _mk(tmp_path)
    assert CONTEXT_LIMIT == 256_000
    assert v.capacity == 2 * CONTEXT_LIMIT == 512_000
    assert v.percent() == 100.0
    assert v.remaining() == 512_000.0


def test_drain_main_thinking_vs_not_exact_units(tmp_path):
    v = _mk(tmp_path)
    # 1000 tokens × temperature 1.2 × thinking(2.0) = 2400 units
    got = v.drain_main(1000, thinking=True, temperature=1.2)
    assert got == 512_000 - 2400
    # 1000 tokens × 1.2 × no-thinking(1.0) = 1200 units
    got = v.drain_main(1000, thinking=False, temperature=1.2)
    assert got == 512_000 - 2400 - 1200
    assert DRAIN_THINKING_FACTOR == 2.0


def test_drain_small_is_half_rate(tmp_path):
    v = _mk(tmp_path)
    # 1000 × 0.5 = 500
    assert v.drain_small(1000) == 512_000 - 500
    assert DRAIN_SMALL_FACTOR == 0.5


def test_charge_rates_asleep_vs_awake_and_clamp(tmp_path):
    v = _mk(tmp_path)
    v.drain_main(512_000, thinking=False, temperature=1.0)  # empty
    assert v.remaining() == 0.0
    # asleep: 0.5%/min → 10 min = 5% of 512000
    v.charge(10, asleep=True)
    after_asleep = round(512_000 * CHARGE_ASLEEP_PER_MIN * 10)
    assert v.remaining() == after_asleep
    # awake: 0.05%/min → 10 min = 0.5% of 512000
    v.charge(10, asleep=False)
    assert v.remaining() == after_asleep + round(512_000 * CHARGE_AWAKE_PER_MIN * 10)
    # clamp at capacity
    v.charge(10_000_000, asleep=True)
    assert v.remaining() == 512_000.0
    assert v.percent() == 100.0


def test_charge_drains_save_automatically(tmp_path):
    v = _mk(tmp_path)
    v.drain_main(1000, thinking=False, temperature=1.0)
    raw = json.loads((tmp_path / "state" / "vitals.json").read_text("utf-8"))
    assert raw["units"] == 512_000.0 - 1000.0


def test_warn_threshold_at_25_percent_remaining(tmp_path):
    v = _mk(tmp_path)
    # drain to just above 25% remaining → no warn
    v.drain_main(int(512_000 * 0.74), thinking=False, temperature=1.0)
    assert v.percent() > BATTERY_WARN_PERCENT
    assert v.should_warn() is False
    # cross to ≤25% remaining → warn
    v.drain_main(int(512_000 * 0.02), thinking=False, temperature=1.0)
    assert v.percent() <= BATTERY_WARN_PERCENT
    assert v.should_warn() is True


def test_forced_off_latch_blocks_and_recovers_at_30_percent(tmp_path):
    v = _mk(tmp_path)
    v.drain_main(512_000, thinking=False, temperature=1.0)  # empty → latch
    assert v.forced_off is True
    assert v.remaining() == 0.0
    # charge back to 29% → still latched
    v.charge(29.0 / (CHARGE_ASLEEP_PER_MIN * 100.0), asleep=True)
    assert v.percent() < BATTERY_RECOVER_PERCENT
    assert v.forced_off is True
    assert v.can_resume() is False
    # charge past 30% → latch lifts automatically (charge() and can_resume())
    v.charge(1.0 / (CHARGE_ASLEEP_PER_MIN * 100.0), asleep=True)
    assert v.percent() >= BATTERY_RECOVER_PERCENT
    assert v.forced_off is False
    assert v.can_resume() is True
    # user messages keep draining even while latched — nothing blocks drain
    v2 = _mk(tmp_path / "b")
    v2.drain_main(512_000, thinking=False, temperature=1.0)
    assert v2.forced_off is True
    v2.drain_main(100, thinking=False, temperature=1.0)  # must not raise
    assert v2.remaining() == 0.0


def test_recover_threshold_is_30_percent_not_warn(tmp_path):
    v = _mk(tmp_path)
    v.drain_main(512_000, thinking=False, temperature=1.0)
    v.charge(25.0 / (CHARGE_ASLEEP_PER_MIN * 100.0), asleep=True)  # 25% < 30%
    assert v.percent() == BATTERY_WARN_PERCENT
    assert v.can_resume() is False
    v.charge(6.0 / (CHARGE_ASLEEP_PER_MIN * 100.0), asleep=True)   # → 31%
    assert v.can_resume() is True


# ─── 血量 (HP) ─────────────────────────────────────────────────────────

def test_hp_damage_clamp_and_died_once(tmp_path):
    v = _mk(tmp_path)
    assert v.hp == 100.0
    assert v.damage(30.0, "测试伤") is False
    assert v.hp == 70.0
    # overkill clamps at exactly 0
    assert v.damage(999.0, "致命伤") is True
    assert v.hp == 0.0
    assert v.died is True
    # second damage call: no death returned again (latched)
    assert v.damage(10.0, "补刀") is False
    assert v.hp == 0.0
    assert v.hp_lost_total == 100.0


def test_heal_is_reserved_noop_stub(tmp_path):
    v = _mk(tmp_path)
    v.damage(50.0, "伤")
    v.heal(50.0, "回血方式后补")
    assert v.hp == 50.0  # untouched


def test_graveyard_archive_moves_files_and_resets(tmp_path):
    mem = tmp_path / "memories"
    (mem / "记忆").mkdir(parents=True)
    (mem / "记忆" / "core.md").write_text("核心记忆", encoding="utf-8")
    (mem / "记忆.md").write_text("# 记忆", encoding="utf-8")
    (mem / "events.json").write_text("[]", encoding="utf-8")
    (mem / "dstate.json").write_text("{}", encoding="utf-8")
    (mem / "mood.json").write_text("{}", encoding="utf-8")
    (mem / "body-changes.json").write_text("[]", encoding="utf-8")
    (mem / "conversations").mkdir()
    (mem / "conversations" / "c1.json").write_text("{}", encoding="utf-8")
    (mem / "ledger.json").with_suffix(".json").write_text(
        json.dumps({"seq": 7, "round_count": 502, "entries": [{"seq": 1, "kind": "fast", "ts": "", "text": "x"}]}),
        encoding="utf-8",
    )
    (mem / "plugins").mkdir()
    (mem / "plugins" / "keep.py").write_text("# body=hardware", encoding="utf-8")
    (mem / "config.json").write_text("{}", encoding="utf-8")

    v = _mk(tmp_path / "state")
    v.damage(100.0, "油尽灯枯")
    assert v.died is True
    grave = v.graveyard_archive(mem)

    assert grave.name.startswith("_grave-")
    # everything listed moved into the grave
    for name in ("events.json", "记忆", "记忆.md", "dstate.json", "mood.json",
                 "body-changes.json", "conversations"):
        assert (grave / name).exists(), name
        assert not (mem / name).exists(), name
    assert (grave / "记忆" / "core.md").read_text("utf-8") == "核心记忆"
    # final death note
    note = json.loads((grave / "_death-note.json").read_text("utf-8"))
    assert note["hp_lost_total"] == 100.0
    assert note["last_reason"] == "油尽灯枯"
    assert note["death_ts"]
    # ledger stays in place, content reset, seq/round_count preserved
    kept = json.loads((mem / "ledger.json").read_text("utf-8"))
    assert kept["entries"] == []
    assert kept["seq"] == 7 and kept["round_count"] == 502
    # plugins + config untouched
    assert (mem / "plugins" / "keep.py").read_text("utf-8") == "# body=hardware"
    assert (mem / "config.json").exists()
    # vitals.json still on disk, state reset to fresh
    v2 = _mk(tmp_path / "state")
    assert v2.hp == 100.0 and v2.died is False and not v2.is_asleep()
    assert v2.remaining() == 512_000.0


def test_graveyard_archive_tolerates_missing_files(tmp_path):
    mem = tmp_path / "memories"
    mem.mkdir()
    v = _mk(tmp_path / "state")
    v.damage(100.0, "突然死亡")
    grave = v.graveyard_archive(mem)
    assert grave.is_dir()
    assert (grave / "_death-note.json").exists()


def test_graveyard_archive_refuses_when_alive(tmp_path):
    v = _mk(tmp_path)
    try:
        v.graveyard_archive(tmp_path / "memories")
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass


# ─── 睡眠 (半梦半醒) ───────────────────────────────────────────────────

def test_forced_sleep_is_exactly_8h_self_min_4h(tmp_path):
    v = _mk(tmp_path)
    v.start_sleep(100, forced=True, reason="用户强制")
    assert v.sleep["planned_s"] == FORCE_SLEEP_S == 8 * 3600
    assert v.sleep["decided_by"] == "forced"
    assert v.is_asleep() is True

    v2 = _mk(tmp_path / "b")
    v2.start_sleep(100, forced=False, reason="自己想睡")  # clamps UP to 4h
    assert v2.sleep["planned_s"] == SELF_SLEEP_MIN_S == 4 * 3600
    assert v2.sleep["decided_by"] == "self"

    v3 = _mk(tmp_path / "c")
    v3.start_sleep(6 * 3600)  # longer request is honored
    assert v3.sleep["planned_s"] == 6 * 3600


def test_start_sleep_noop_while_already_asleep(tmp_path):
    v = _mk(tmp_path)
    v.start_sleep(5 * 3600)
    again = v.start_sleep(7 * 3600)
    assert again["planned_s"] == 5 * 3600


def test_stage_ladder_and_think_flag(tmp_path, monkeypatch):
    _fake_epoch(monkeypatch)
    v = _mk(tmp_path)
    v.start_sleep(4 * 3600)
    v.user_speech_wake(_s(600))
    assert v.sleep["wakes"] == 1
    r2 = v.user_speech_wake(_s(1200))
    assert r2["stage"] == 2 and r2["think"] is True and r2["allow_reply"] is True
    r3 = v.user_speech_wake(_s(1800))
    assert r3["stage"] == 3 and r3["think"] is True
    r4 = v.user_speech_wake(_s(2400))
    assert r4["stage"] == 3
    # four wake calls in this test → 4 × 1.0 HP lost, reason "被吵醒"
    assert v.hp == 96.0
    raw = json.loads((tmp_path / "state" / "vitals.json").read_text("utf-8"))
    assert raw["sleep"]["hp_lost_episode"] == 4.0  # 4 wakes in this test
    assert raw["last_damage"]["reason"] == "被吵醒"


def test_stage3_fast_activation_is_random_and_persists(tmp_path, monkeypatch):
    # random.random() after random.seed(1) → 0.134… (<0.5 → activate);
    # after random.seed(2) → 0.823… (≥0.5 → stay off)
    for seed, expect in ((1, True), (2, False)):
        random.seed(seed)
        _fake_epoch(monkeypatch)
        v = _mk(tmp_path / f"seed{seed}")
        v.start_sleep(4 * 3600)
        v.user_speech_wake(_s(1))
        v.user_speech_wake(_s(2))
        r3 = v.user_speech_wake(_s(3))
        assert v.sleep["stage"] == 3
        assert r3["fast_activated"] is expect
        assert v.sleep["fast_activated"] is expect
        # persists on later wakes — no re-roll
        r4 = v.user_speech_wake(_s(4))
        assert r4["fast_activated"] is expect


def test_multiplier_example_planned_4h_slept_1h_first_wake(tmp_path, monkeypatch):
    _fake_epoch(monkeypatch)
    v = _mk(tmp_path)
    v.start_sleep(4 * 3600)
    r = v.user_speech_wake(_s(3600))  # slept 1h, then one 吵醒
    # multiplier = clamp(1.2 + 1.0 − 1.0, …) = 1.2
    # new remaining = 4h × 1.2 − 1h = 3.8h
    assert r["new_sleep_remaining_s"] == 4 * 3600 * 1.2 - 3600
    assert abs(r["new_sleep_remaining_s"] - 3.8 * 3600) < 1e-6
    # planned_s was updated so the next computation compounds
    assert v.sleep["planned_s"] == 4 * 3600 * 1.2


def test_multiplier_compounds_on_second_wake(tmp_path, monkeypatch):
    _fake_epoch(monkeypatch)
    v = _mk(tmp_path)
    v.start_sleep(4 * 3600)
    v.user_speech_wake(_s(3600))                      # planned → 4h×1.2
    r2 = v.user_speech_wake(_s(7200))                 # slept 2h total
    # hp_lost_episode after 2 wakes = 2.0 → extra = 1.0 → mult = 2.2
    # planned compounds: (4h × 1.2) × 2.2
    planned_now = 4 * 3600 * 1.2 * 2.2
    remaining = planned_now - 7200
    assert r2["new_sleep_remaining_s"] == remaining
    assert v.sleep["planned_s"] == planned_now


def test_multiplier_caps_at_3x(tmp_path, monkeypatch):
    _fake_epoch(monkeypatch)
    v = _mk(tmp_path)
    v.start_sleep(4 * 3600)
    v.user_speech_wake(_s(1))
    # extra damage on top of wakes pushes mult to the 3.0 cap
    v.damage(5.0, "谵妄连击")
    r = v.user_speech_wake(_s(2))
    # hp_lost_episode = 1.0 (wake) + 5.0 = 6.0 → extra 5.0 → 1.2+5 = 6.2 → capped 3.0
    # planned compounds: (4h × 1.2 first wake) × 3.0
    assert v.sleep["planned_s"] == 4 * 3600 * 1.2 * 3.0
    assert r["new_sleep_remaining_s"] == 4 * 3600 * 1.2 * 3.0 - 2
    assert SLEEP_MULT_MAX == 3.0 and SLEEP_MULT_MIN == 1.2 and SLEEP_MULT_BASE == 1.2


def test_intuition_tick_during_sleep_damages_0_1(tmp_path, monkeypatch):
    _fake_epoch(monkeypatch)
    v = _mk(tmp_path)
    v.start_sleep(4 * 3600)
    before = v.hp
    info = v.intuition_tick_during_sleep()
    assert info is not None
    assert info["stage"] == 1
    assert info["somatic"] == SOMATIC_TEXTS[1]
    assert abs(v.hp - (before - 0.1)) < 1e-9
    assert info["hp_lost_episode"] == 0.1
    # not asleep → None
    v2 = _mk(tmp_path / "b")
    assert v2.intuition_tick_during_sleep() is None


def test_intuition_tick_pushes_multiplier_up(tmp_path, monkeypatch):
    _fake_epoch(monkeypatch)
    v = _mk(tmp_path)
    v.start_sleep(4 * 3600)
    v.user_speech_wake(_s(1))                      # episode 1.0 → mult 1.2 → planned 4h×1.2
    v.intuition_tick_during_sleep()                # +0.1 → episode 1.1
    r = v.user_speech_wake(_s(2))                  # wake2 → episode 2.1 → extra 1.1 → mult 2.3×
    assert v.sleep["planned_s"] == 4 * 3600 * 1.2 * 2.3
    assert r["stage"] == 2


def test_somatic_texts_exact_strings_never_numeric(tmp_path):
    for stage, text in SOMATIC_TEXTS.items():
        assert text in (
            "你的身体有点晕，有点头痛，需要睡觉，不然会越来越痛，可能引起危险的后果。",
            "头疼得厉害，身体在发热，再不睡会有危险。",
            "脑子里嗡嗡的，意识忽远忽近……",
        )
        for ch in "0123456789HP血量电量":
            assert ch not in text
    v = _mk(tmp_path)
    v.start_sleep(4 * 3600)
    for wake_n, stage in ((1, 1), (2, 2), (3, 3)):
        r = v.user_speech_wake(_s(wake_n * 60))
        assert r["somatic"] == SOMATIC_TEXTS[stage]


def test_natural_wake_resets_episode_counters(tmp_path, monkeypatch):
    _fake_epoch(monkeypatch)
    v = _mk(tmp_path)
    v.start_sleep(4 * 3600)
    v.user_speech_wake(_s(1))                       # one 吵醒 → hp lost 1.0
    assert v.hp == 99.0
    # timer not yet out
    assert v.natural_wake(_s(100)) is False
    assert v.is_asleep() is True
    # the wake already stretched planned to 4h × 1.2, so 4h+1s is still asleep
    assert v.natural_wake(_s(4 * 3600 + 1)) is False
    assert v.natural_wake(_s(int(4 * 3600 * 1.2) + 1)) is True
    assert v.is_asleep() is False
    # new episode starts clean at stage 1
    v.start_sleep(4 * 3600)
    assert v.sleep["stage"] == 1
    assert v.sleep["hp_lost_episode"] == 0.0
    assert v.sleep["fast_activated"] is False
    assert v.sleep["wakes"] == 0


def test_sleep_only_user_speech_advances_ladder_by_api_shape(tmp_path):
    v = _mk(tmp_path)
    v.start_sleep(4 * 3600)
    # the module exposes exactly one wake entry; nothing else mutates stage
    public = [n for n in dir(Vitals) if not n.startswith("_")]
    wake_ish = [n for n in public if "wake" in n.lower() or "speech" in n.lower()]
    assert wake_ish == ["natural_wake", "user_speech_wake"]
    v.intuition_tick_during_sleep()
    assert v.sleep["stage"] == 1  # intuition never advances the ladder


def test_battery_drains_do_not_touch_sleep_or_hp(tmp_path, monkeypatch):
    _fake_epoch(monkeypatch)
    v = _mk(tmp_path)
    v.start_sleep(4 * 3600)
    v.drain_main(1000, thinking=True, temperature=1.0)
    assert v.sleep["stage"] == 1 and v.hp == 100.0


def test_persistence_roundtrip_keeps_hp_sleep_battery(tmp_path, monkeypatch):
    _fake_epoch(monkeypatch)
    v = _mk(tmp_path)
    v.drain_main(1000, thinking=True, temperature=1.5)   # 1000×1.5×2 = 3000 units
    v.damage(2.5, "摔了一跤")                            # 100 → 97.5
    v.start_sleep(5 * 3600)
    v.user_speech_wake(_s(60))                           # 97.5 → 96.5, stage 1
    fresh = _mk(tmp_path)  # new instance over the same dir
    assert fresh.hp == 96.5
    assert fresh.died is False
    assert fresh.sleep is not None and fresh.sleep["stage"] == 1
    assert fresh.remaining() == 512_000.0 - 3000.0
    assert fresh.is_asleep() is True


def test_persistence_tolerates_missing_and_corrupt(tmp_path):
    v = _mk(tmp_path)  # no file yet → fresh defaults
    assert v.hp == 100.0
    p = tmp_path / "state" / "vitals.json"
    p.write_text("{not json", encoding="utf-8")
    v2 = _mk(tmp_path)
    assert v2.hp == 100.0
