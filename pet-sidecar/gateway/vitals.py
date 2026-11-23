"""Vitals (生命体征) — 电量 + 血量 + 睡眠状态机 for the 凌凌 desk pet.

Battery (电量) is measured in TOKENS: capacity = 2 × context_limit, so the
架构极限 256_000 context maps to a 512_000-unit battery (the mapping follows
the architecture limit and does NOT change with whatever model/LoRA happens
to be loaded). Main-brain rounds drain with temperature × thinking
multipliers; small rounds (直觉 / fast lane) drain at half rate. Sleep and
awake idle charge units back. Warn / forced-off / recovery are all
remaining-based latches — proactive rounds must be blocked by the CALLER
while `forced_off` is latched; user messages are always answered and keep
draining.

HP (血量) is the body. Every user-speech wake during sleep costs 1.0 HP,
every intuition call during the 谵妄 (delirious) sleep stage costs 0.1 HP.
Death latches once; `graveyard_archive()` moves the memories into a
`_grave-*` folder (身体=硬件，记忆=自我: the plugins directory and config
files are NEVER touched) and resets to a fresh body.

Sleep (半梦半醒) is a state machine where ONLY the user speaking advances
the wake ladder (stage 1 → 2 → 3). Drag / touch / agent events never wake
the pet — enforced by API shape: `user_speech_wake()` is the sole wake
entry this module exposes.

Pure stdlib. Store: `<dir>/vitals.json`, written atomically
(same-directory temp file + `os.replace`), on every state change.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

# ─── 电量 (battery, measured in tokens) ────────────────────────────────

# 架构极限上下文（tokens）。映射的是架构上限，不随当前加载的模型/LoRA变化。
CONTEXT_LIMIT = 256_000
# capacity = 2 × context_limit → 默认 512_000 units
BATTERY_CAPACITY_FACTOR = 2.0

# 剩余电量阈值（percent of capacity, remaining-based）。
BATTERY_WARN_PERCENT = 25.0     # 剩余 ≤ 25%（即已耗 75%）→ should_warn()
BATTERY_RECOVER_PERCENT = 30.0  # 强制关机闩锁：充回 ≥ 30% 才允许恢复主动行为

# drain 乘数。
DRAIN_THINKING_FACTOR = 2.0     # 思考轮（thinking）耗电 ×2
DRAIN_SMALL_FACTOR = 0.5        # 小轮（直觉 / fast lane，无温度）耗电 ×0.5

# charge 速率（每分钟恢复的容量比例）。
CHARGE_ASLEEP_PER_MIN = 0.005   # 睡觉充电：0.5% / min
CHARGE_AWAKE_PER_MIN = 0.0005   # 清醒挂机充电：0.05% / min

# ─── 血量 (HP) ─────────────────────────────────────────────────────────

HP_MAX = 100.0
WAKE_DAMAGE_HP = 1.0            # 睡梦中被用户说话吵醒，每次扣 1.0 HP
INTUITION_DAMAGE_HP = 0.1       # 谵妄期的每一次直觉调用扣 0.1 HP

# ─── 睡眠 (半梦半醒) ───────────────────────────────────────────────────

FORCE_SLEEP_S = 8 * 3600        # 被强制睡觉：固定 8 小时（入参被钳到这个值）
SELF_SLEEP_MIN_S = 4 * 3600     # 自己决定睡觉：最少 4 小时（入参被钳到下限）

# 吵醒倍率：planned_s ×= multiplier（复利累乘）。
# 标准吵醒（损耗 1.0 HP）= 1.2×；之后每额外 1.0 HP 损耗 +1.0×，上限 3.0×
# （谵妄期每 0.1 HP 的直觉调用也会把倍率往上顶）。
# 注：产品示例「扣0.2→1.3」与本实现相差的是 0.1-HP 直觉调用的取整粒度；
# 以下常数全部在本文件顶部，可调。
SLEEP_MULT_BASE = 1.2
SLEEP_MULT_MIN = 1.2
SLEEP_MULT_MAX = 3.0

# 身体感受文案（按 stage 取键；永不提 HP / 数字）。
SOMATIC_TEXTS: Dict[int, str] = {
    1: "你的身体有点晕，有点头痛，需要睡觉，不然会越来越痛，可能引起危险的后果。",
    2: "头疼得厉害，身体在发热，再不睡会有危险。",
    3: "脑子里嗡嗡的，意识忽远忽近……",
}

# 入葬时搬进 _grave-* 的身体/记忆文件（身体=硬件，记忆=自我；
# 插件目录与配置文件永不触碰）。
GRAVE_MOVED_ITEMS = (
    "events.json",
    "记忆",            # whole directory
    "记忆.md",
    "dstate.json",
    "mood.json",
    "body-changes.json",
    "conversations",   # whole directory
)


# ─── helpers ───────────────────────────────────────────────────────────

def _to_epoch(now: Union[None, float, datetime]) -> float:
    """Accept None (real now), a float epoch, or a datetime."""
    if now is None:
        return time.time()
    if isinstance(now, datetime):
        return now.timestamp()
    return float(now)


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


class Vitals:
    """电量 + 血量 + 睡眠 — single source of truth, persisted to vitals.json.

    Every state-changing call saves atomically; callers never need to
    remember to call save(). The store file is `<state_dir>/vitals.json`.
    """

    def __init__(self, state_dir: Union[str, Path], *, context_limit: int = CONTEXT_LIMIT) -> None:
        self._dir = Path(state_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "vitals.json"
        self._lock = threading.Lock()
        self._context_limit = max(1, int(context_limit))
        self._capacity = float(self._context_limit) * BATTERY_CAPACITY_FACTOR
        # 电量
        self._units = self._capacity
        self._forced_off = False
        # 血量
        self._hp = HP_MAX
        self._died = False
        self._hp_lost_total = 0.0
        self._death_ts: Optional[str] = None
        self._last_damage: Optional[Dict[str, Any]] = None
        # 睡眠
        self._sleep: Optional[Dict[str, Any]] = None
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
            units = data.get("units")
            if isinstance(units, (int, float)):
                self._units = min(self._capacity, max(0.0, float(units)))
            self._forced_off = bool(data.get("forced_off"))
            hp = data.get("hp")
            if isinstance(hp, (int, float)):
                self._hp = min(HP_MAX, max(0.0, float(hp)))
            self._died = bool(data.get("died"))
            lost = data.get("hp_lost_total")
            if isinstance(lost, (int, float)):
                self._hp_lost_total = max(0.0, float(lost))
            self._death_ts = data.get("death_ts") if isinstance(data.get("death_ts"), str) else None
            last = data.get("last_damage")
            self._last_damage = dict(last) if isinstance(last, dict) else None
            sleep = data.get("sleep")
            self._sleep = dict(sleep) if isinstance(sleep, dict) else None

    def save(self) -> None:
        """Persist atomically (tmp file + os.replace)."""
        with self._lock:
            self._write()

    def _write(self) -> None:
        # caller must hold the lock
        _atomic_write_json(self._path, {
            "version": 1,
            "context_limit": self._context_limit,
            "capacity": self._capacity,
            "units": self._units,
            "forced_off": self._forced_off,
            "hp": self._hp,
            "died": self._died,
            "hp_lost_total": self._hp_lost_total,
            "death_ts": self._death_ts,
            "last_damage": self._last_damage,
            "sleep": self._sleep,
        })

    # ------------------------------------------------------------------
    # 电量 (battery)
    # ------------------------------------------------------------------

    @property
    def capacity(self) -> float:
        return self._capacity

    @property
    def forced_off(self) -> bool:
        """Latched forced-off: the CALLER must block proactive rounds."""
        return self._forced_off

    def drain_main(self, tokens: int, thinking: bool, temperature: float) -> float:
        """Main-brain round: units = tokens × temperature × (2.0 if thinking else 1.0)."""
        units = float(tokens) * float(temperature) * (DRAIN_THINKING_FACTOR if thinking else 1.0)
        return self._drain(units)

    def drain_small(self, tokens: int) -> float:
        """Small round (直觉/fast lane, no temperature): units = tokens × 0.5."""
        return self._drain(float(tokens) * DRAIN_SMALL_FACTOR)

    def _drain(self, units: float) -> float:
        if units <= 0.0:
            return self.remaining()
        with self._lock:
            self._units = max(0.0, self._units - units)
            if self._units <= 0.0:
                # 剩余 ≤ 0 → 强制关机闩锁（只能靠 can_resume()/充电解除）
                self._forced_off = True
            self._write()
            return self._units

    def remaining(self) -> float:
        with self._lock:
            return self._units

    def percent(self) -> float:
        with self._lock:
            return self._percent_unlocked()

    def _percent_unlocked(self) -> float:
        if self._capacity <= 0.0:
            return 0.0
        return min(100.0, max(0.0, self._units / self._capacity * 100.0))

    def should_warn(self) -> bool:
        """剩余 ≤ 25%（已耗 75%）→ 低电量提醒该注入下一轮 system prompt。"""
        return self.percent() <= BATTERY_WARN_PERCENT

    def can_resume(self) -> bool:
        """Recovery latch check — and the recovery point.

        While `forced_off` is latched this stays False until the battery is
        charged back to ≥ 30%; crossing the threshold here (or inside
        charge()) lifts the latch.
        """
        with self._lock:
            if not self._forced_off:
                return True
            if self._percent_unlocked() >= BATTERY_RECOVER_PERCENT:
                self._forced_off = False
                self._write()
                return True
            return False

    def charge(self, minutes: float, asleep: bool) -> float:
        """asleep: +0.5%/min, awake idle: +0.05%/min, clamped at capacity."""
        rate = CHARGE_ASLEEP_PER_MIN if asleep else CHARGE_AWAKE_PER_MIN
        with self._lock:
            self._units = min(
                self._capacity,
                self._units + self._capacity * rate * float(minutes),
            )
            if self._forced_off and self._percent_unlocked() >= BATTERY_RECOVER_PERCENT:
                # 充回恢复阈值 → 闩锁自动解除（与 can_resume() 同一语义）
                self._forced_off = False
            self._write()
            return self._units

    # ------------------------------------------------------------------
    # 血量 (HP)
    # ------------------------------------------------------------------

    @property
    def hp(self) -> float:
        return self._hp

    @property
    def died(self) -> bool:
        return self._died

    @property
    def hp_lost_total(self) -> float:
        """Lifetime HP lost (used by the death note; reset by graveyard_archive)."""
        return self._hp_lost_total

    def damage(self, amount: float, reason: str = "") -> bool:
        """Apply damage (clamped at 0 HP). Returns True iff HP hit 0 THIS call.

        While asleep the lost HP also feeds the sleep episode's
        ``hp_lost_episode`` (吵醒倍率按本次睡眠累计损耗计算).
        """
        with self._lock:
            was_dead = self._died
            lost, died_now = self._damage_unlocked(amount, reason)
            if not was_dead or lost > 0.0:
                self._write()
            return died_now

    def _damage_unlocked(self, amount: float, reason: str) -> Tuple[float, bool]:
        # caller must hold the lock
        if self._died:
            return 0.0, False
        amount = max(0.0, float(amount))
        prev = self._hp
        self._hp = max(0.0, prev - amount)
        # round so 0.1-HP 谵妄调用 accumulate as clean decimals (100.0−0.1 drifts in binary float)
        lost = round(prev - self._hp, 6)
        self._hp_lost_total = round(self._hp_lost_total + lost, 6)
        if self._sleep is not None and lost > 0.0:
            # the CURRENT sleep episode's loss drives the 吵醒倍率;
            # folded in here so damage() AND the in-sleep callers share it
            self._sleep["hp_lost_episode"] = round(
                float(self._sleep.get("hp_lost_episode", 0.0)) + lost, 6
            )
        self._last_damage = {"amount": amount, "reason": reason, "ts": _utc_iso()}
        died_now = False
        if self._hp <= 0.0:
            self._died = True
            self._death_ts = _utc_iso()
            died_now = True
        return lost, died_now

    def heal(self, amount: float, source: str) -> None:
        """Reserved stub — 回血方式后补（目前没有任何调用方）."""
        return None

    def graveyard_archive(self, memory_dir: Union[str, Path]) -> Path:
        """Move this life's body+memories into `memory_dir/_grave-<stamp>/`.

        Moves: events.json, 记忆/, 记忆.md, dstate.json, mood.json,
        body-changes.json, conversations/. ledger.json + vitals.json STAY in
        place but get their content fields reset. Plugins and config files
        are never touched (身体=硬件，记忆=自我). Tolerates missing files.
        After archiving, in-memory state resets to fresh and saves.
        """
        if not self._died:
            raise RuntimeError("宠物尚未死亡，拒绝入葬")
        memory_dir = Path(memory_dir)
        memory_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        grave = memory_dir / f"_grave-{stamp}"
        grave.mkdir(parents=True, exist_ok=True)

        # 1) move the listed artifacts (tolerate missing files)
        for name in GRAVE_MOVED_ITEMS:
            src = memory_dir / name
            if src.exists():
                shutil.move(str(src), str(grave / name))

        # 2) final death note
        with self._lock:
            note = {
                "hp_lost_total": self._hp_lost_total,
                "death_ts": self._death_ts,
                "last_reason": (self._last_damage or {}).get("reason", ""),
            }
        _atomic_write_json(grave / "_death-note.json", note)

        # 3) ledger.json stays in place, content reset (seq / round_count preserved)
        self._reset_ledger_file(memory_dir)

        # 4) fresh body, in memory and on disk
        with self._lock:
            self._units = self._capacity
            self._forced_off = False
            self._hp = HP_MAX
            self._died = False
            self._hp_lost_total = 0.0
            self._death_ts = None
            self._last_damage = None
            self._sleep = None
            self._write()
        return grave

    @staticmethod
    def _reset_ledger_file(memory_dir: Path) -> None:
        """Reset the sibling ledger.json in place, keeping seq/round_count."""
        path = memory_dir / "ledger.json"
        if not path.exists():
            return
        seq, round_count = 0, 0
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            seq = int(old.get("seq") or 0)
            round_count = int(old.get("round_count") or 0)
        except (OSError, ValueError):
            pass
        _atomic_write_json(path, {
            "version": 1,
            "seq": seq,
            "round_count": round_count,
            "entries": [],
            "purge_fast_quota": 0,
            "purge_slow_quota": 0,
            "pending_recap": False,
            "recap_text": None,
        })

    # ------------------------------------------------------------------
    # 睡眠 (半梦半醒)
    # ------------------------------------------------------------------

    @property
    def sleep(self) -> Optional[Dict[str, Any]]:
        """Copy of the current sleep episode state, or None when awake."""
        with self._lock:
            return dict(self._sleep) if self._sleep else None

    @property
    def sleep_stage(self) -> Optional[int]:
        with self._lock:
            return int(self._sleep["stage"]) if self._sleep else None

    def is_asleep(self) -> bool:
        return self._sleep is not None

    def start_sleep(self, duration_s: float, forced: bool = False, reason: str = "") -> Dict[str, Any]:
        """Enter sleep. forced → exactly 8h; self-decided → at least 4h.

        No-op (returns the running episode) if already asleep. A brand-new
        episode starts at stage 1 with clean counters.
        """
        with self._lock:
            if self._sleep is not None:
                return dict(self._sleep)
            if forced:
                planned = float(FORCE_SLEEP_S)
            else:
                planned = max(float(duration_s), float(SELF_SLEEP_MIN_S))
            self._sleep = {
                "started_at": time.time(),
                "planned_s": planned,
                "stage": 1,
                "wakes": 0,
                "hp_lost_episode": 0.0,
                "fast_activated": False,
                "decided_by": "forced" if forced else "self",
                "reason": reason,
            }
            self._write()
            return dict(self._sleep)

    def sleep_remaining_s(self, now: Union[None, float, datetime] = None) -> float:
        with self._lock:
            s = self._sleep
            if s is None:
                return 0.0
            elapsed = max(0.0, _to_epoch(now) - float(s["started_at"]))
            return max(0.0, float(s["planned_s"]) - elapsed)

    def user_speech_wake(self, now: Union[None, float, datetime] = None) -> Optional[Dict[str, Any]]:
        """The ONLY user-initiated wake entry (drag/touch/agent never wake).

        Each call = one 吵醒 = damage(1.0, "被吵醒") (no-op when already
        dead → returns None). First wake keeps stage 1, second → stage 2,
        third+ → stage 3 (rolling random() < 0.5 on ENTERING stage 3 latches
        fast_activated for the episode). Sleep plan compounds:
        planned_s ×= clamp(1.2 + max(0, hp_lost_episode − 1.0), 1.2, 3.0).

        Returns {stage, allow_reply, think, fast_activated, somatic,
        new_sleep_remaining_s} for the caller, or None when not asleep/dead.
        """
        with self._lock:
            s = self._sleep
            if s is None or self._died:
                return None
            s["wakes"] = int(s.get("wakes", 0)) + 1
            # _damage_unlocked folds the loss into hp_lost_episode
            self._damage_unlocked(WAKE_DAMAGE_HP, "被吵醒")

            if s["wakes"] >= 3:
                if int(s["stage"]) < 3:
                    s["stage"] = 3
                    # entering stage 3: one roll, result latches for the episode
                    if random.random() < 0.5:
                        s["fast_activated"] = True
                # already at stage 3: no new roll; fast_activated persists
            elif s["wakes"] == 2:
                s["stage"] = max(int(s["stage"]), 2)
            # wakes == 1 → stage stays 1

            stage = int(s["stage"])
            extra = max(0.0, float(s["hp_lost_episode"]) - WAKE_DAMAGE_HP)
            mult = min(SLEEP_MULT_MAX, max(SLEEP_MULT_MIN, SLEEP_MULT_BASE + extra))
            already = max(0.0, _to_epoch(now) - float(s["started_at"]))
            s["planned_s"] = float(s["planned_s"]) * mult  # compounds next time
            remaining = max(0.0, float(s["planned_s"]) - already)
            self._write()
            return {
                "stage": stage,
                "allow_reply": True,
                "think": stage >= 2,
                "fast_activated": bool(s["fast_activated"]),
                "somatic": SOMATIC_TEXTS[stage],
                "new_sleep_remaining_s": remaining,
            }

    def intuition_tick_during_sleep(self) -> Optional[Dict[str, Any]]:
        """One intuition call while asleep: damages 0.1 HP (谵妄期的每一次直觉调用).

        Returns the current stage info, or None when not asleep. Whether
        stage ≥ 3 / fast_activated gates the intuition call at all is the
        caller's decision.
        """
        with self._lock:
            s = self._sleep
            if s is None:
                return None
            # _damage_unlocked folds the loss into hp_lost_episode
            self._damage_unlocked(INTUITION_DAMAGE_HP, "谵妄期的每一次直觉调用")
            stage = int(s["stage"])
            self._write()
            return {
                "stage": stage,
                "fast_activated": bool(s["fast_activated"]),
                "somatic": SOMATIC_TEXTS[stage],
                "hp_lost_episode": float(s["hp_lost_episode"]),
            }

    def natural_wake(self, now: Union[None, float, datetime] = None) -> bool:
        """Sleep timer ran out → exit sleep, reset episode counters. True iff woke."""
        with self._lock:
            if self._sleep is None:
                return False
            s = self._sleep
            elapsed = max(0.0, _to_epoch(now) - float(s["started_at"]))
            if float(s["planned_s"]) - elapsed > 0.0:
                return False
            self._sleep = None  # episode counters die with the episode
            self._write()
            return True
