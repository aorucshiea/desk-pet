"""Integration wiring: the reflex gate (快直觉, System 0) in the chat pipeline.

Covers the reflex-wiring contract:
- proactive D-quiet rounds with an armed reflex layer go through
  ``reflex.judge``; a reflex quiet (think=false) short-circuits with
  start → tempo → next_check_s → quiet → inner → end, no provider call,
  one small drain (documented constant: 300 prompt tokens + max_tokens),
  ONE fast ledger entry "<action> <reason>", one round_tick, and the
  finally block does nothing (no drain_main, no second tick, no slow
  entry);
- a reflex escalation (any non-quiet action, or think=true) falls through
  into the full pipeline: the provider IS called, the finally block drains
  the main battery / ticks / records the slow entry, and a next_check_s
  frame is emitted right before the final end frame;
- a reflex escalation VETOES D's quiet (D bookkeeping still recorded);
  a reflex quiet eats D's speak (short-circuit, no provider);
- with the reflex layer absent or disabled, the old D-quiet early-return
  behaviour is preserved verbatim (no judge call, no drain_small, no fast
  entry, telemetry counters untouched);
- judge returning source="fallback" is treated exactly like a reflex
  quiet round, with the fallback telemetry counter incremented;
- asleep proactive rounds keep their pre-D short-circuit (judge never
  called); real user turns never reach the gate;
- the EMITTED next_check_s and the STATE tempo_mode parity: emitted
  seconds are always inside server._TEMPO_SPECS[mode]["interval_s"],
  and reflex.TEMPO_RANGE_S mirrors _TEMPO_SPECS exactly.

The harness follows test_gateway_vitals_wiring.py / test_gateway_cortex.py:
a scripted OpenAIProvider subclass drives the real ``_stream_chat_provider``,
the life singletons are installed via ``set_vitals`` / ``set_ledger``, and
the reflex layer is armed per-test via the request-scoped override
(``set_reflex_config_override``) so no app boot is needed and nothing leaks
into the boot singleton other tests read.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

import pytest

from gateway import brain as brain_mod  # noqa: F401 (subscribe side effects)
from gateway import server as server_mod
from gateway import telemetry as telemetry_mod
from gateway.decision import cortex
from gateway.decision import reflex
from gateway.decision.cortex import DStateStore
from gateway.decision.reflex import ReflexConfig
from gateway.ledger import Ledger
from gateway.providers.openai import OpenAIProvider
from gateway.server import ChatRequest
from gateway.vitals import Vitals


# ── Stubs (same shape as test_gateway_vitals_wiring.py) ─────────────


@pytest.fixture(autouse=True)
def _isolate_life_state(monkeypatch, tmp_path):
    monkeypatch.setattr(server_mod, "_dstate_provider", None)
    monkeypatch.setattr(server_mod, "_body_log_ref", None)
    monkeypatch.setattr(server_mod, "_ephemeral_ref", None)
    monkeypatch.setattr(server_mod, "_last_user_msg", "")
    monkeypatch.setattr(server_mod, "_last_user_ts", 0.0)
    monkeypatch.setattr(server_mod, "_last_real_user_ts", 0.0)
    # Fresh tempo + density + pending-vitals state per test.
    server_mod._TEMPO_STATE.clear()
    server_mod._TEMPO_STATE.update({"mode": "", "expires_at": 0.0, "source": ""})
    server_mod._DENSITY_STATE.clear()
    server_mod._DENSITY_STATE.update({"last_round_at": 0.0, "last_user_turn_at": 0.0})
    server_mod._DENSITY_EVENTS.clear()
    server_mod._pending_vitals_round.clear()
    server_mod._pending_vitals_round.update({"organize_kind": None, "overflow": False})
    # The time-sense latch is module state: a previous test asking for the
    # clock must never leak a current_time line into the next test's state.
    server_mod._REFLEX_TIME_STATE.clear()
    server_mod._REFLEX_TIME_STATE.update({"wanted": False, "asked_at": 0.0})
    # Never let a built app's boot singleton (or a previous test's shadow)
    # decide whether this test's reflex gate is armed.
    server_mod._reflex_config_ref.pop("cfg", None)
    server_mod.clear_reflex_config_override()
    brain_mod.clear()
    telemetry_mod._instance.counters.clear()
    telemetry_mod._instance.gauges.clear()
    yield
    server_mod.clear_reflex_config_override()


class _RecordingOpenAI(OpenAIProvider):
    """Real OpenAIProvider surface, no HTTP: records kwargs + script."""

    def __init__(self, script: list[list[dict[str, Any]]]) -> None:
        super().__init__(
            base_url="https://api.example.com/v1",
            model="deepseek-chat",
            name="deepseek",
            thinking=True,
            reasoning_effort="max",
        )
        self.script = [list(turn) for turn in script]
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        index = min(len(self.calls) - 1, len(self.script) - 1)
        for event in self.script[index]:
            yield dict(event)


class _StubMCP:
    async def call_tool(self, server_name, tool_name, arguments):
        return {"summary": "ok"}


class _StubBridge:
    def new_session(self) -> None: ...

    def post(self, *_a, **_k) -> None: ...


class _StubMood:
    emotion_index = 0.0

    def format_for_system_prompt(self) -> str:
        return ""

    def apply_emotion_tag(self, _tag) -> None: ...


class _StubRegistry:
    def __init__(self, provider) -> None:
        self._providers = {provider.name: provider}
        self._default = provider

    def get(self, name):
        return self._providers.get(name, self._default)


def _decision(**overrides) -> dict:
    value = {
        "decision_id": "d_test",
        "thinking": False,
        "reasoning_effort": None,
        "strength": "low",
        "reaction": "small",
        "max_tokens": 2048,
        "action": "speak",
        "action_label": "说",
        "source": "test",
        "confidence": 0.75,
        "reason": "test|action=speak",
    }
    value.update(overrides)
    if "action" in overrides and "action_label" not in overrides:
        value["action_label"] = next(
            (option["label"] for option in cortex.DEFAULT_ACTION_OPTIONS
             if option["id"] == value["action"]),
            value["action"],
        )
    return value


def _install(monkeypatch, provider, *, passive=None):
    monkeypatch.setattr(server_mod, "_resolve_provider", lambda *_a, **_k: provider)
    monkeypatch.setattr(server_mod, "_kernel", lambda: None)
    monkeypatch.setattr(server_mod, "get_event_store", lambda: None)
    monkeypatch.setattr(server_mod, "_extraction_hook", None)

    async def _no_tools(*_a, **_k):
        return None

    monkeypatch.setattr(server_mod, "_gather_tools", _no_tools)
    if passive is not None:
        monkeypatch.setattr(cortex, "passive_decision", passive)


def _passive(**overrides):
    """Async passive_decision stub (the real one is awaited)."""

    async def _decide(*_a, **_k):
        return _decision(**overrides)

    return _decide


def _run(provider, req) -> list[dict]:
    async def _drain():
        frames: list[dict] = []
        async for chunk in server_mod._stream_chat_provider(
            _StubRegistry(provider),
            _StubMCP(),
            _StubBridge(),
            req,
            server=None,
            server_state={},
            mood_store=_StubMood(),
            lora_arr=None,
        ):
            text = chunk.decode("utf-8")
            if text.startswith("data: "):
                frames.append(json.loads(text[6:]))
        return frames

    return asyncio.run(_drain())


def _req(**kwargs) -> ChatRequest:
    defaults = {
        "messages": [{"role": "user", "content": "今天怎么样"}],
        "max_new_tokens": 4096,
        "thinking": False,
        "tools_enabled": False,
    }
    defaults.update(kwargs)
    return ChatRequest(**defaults)


_PROACTIVE = [{"role": "user", "content": "[系统提示：该说话了]"}]


def _visible(frames: list[dict]) -> str:
    return "".join(
        frame.get("content", "")
        for frame in frames
        if frame.get("event") == "delta"
    )


def _events(frames: list[dict]) -> list[str]:
    return [f.get("event") for f in frames]


def _next_check_frames(frames: list[dict]) -> list[dict]:
    return [f for f in frames if f.get("event") == "next_check_s"]


# ── Fixtures: life singletons + reflex arming ────────────────────────


@pytest.fixture
def life(tmp_path, monkeypatch):
    """Fresh Vitals + Ledger singletons installed for the chat stream.

    State dirs live in tmp (conftest also pins PET_MEMORY_DIR there), and
    ``set_vitals`` / ``set_ledger`` are undone after the test so a built
    app's own singletons never leak between tests.
    """
    state_dir = tmp_path / "life"
    vitals = Vitals(state_dir, context_limit=1000)  # small battery for tests
    ledger = Ledger(state_dir)
    server_mod.set_vitals(vitals)
    server_mod.set_ledger(ledger)
    server_mod._vitals_ref["dir"] = state_dir
    try:
        yield vitals, ledger
    finally:
        server_mod.set_vitals(None)
        server_mod.set_ledger(None)
        server_mod._vitals_ref.pop("dir", None)


def _arm(monkeypatch, judged: dict, *, max_tokens: int = 128) -> list[dict]:
    """Arm the reflex layer with a scripted judge; returns the call log.

    The fake judge is the exact seam the wiring owns: it records the state
    dicts it was shown and replays ``judged`` verbatim, so dispatch can be
    tested without HTTP. The ReflexConfig is enabled and carries the given
    max_tokens (the small-drain constant depends on it).
    """
    calls: list[dict] = []

    async def _judge(state, config, **kwargs):
        calls.append({"state": dict(state), "config": config})
        return dict(judged)

    monkeypatch.setattr(
        server_mod._reflex_module, "judge", _judge, raising=False
    )
    server_mod.set_reflex_config_override(
        ReflexConfig(
            url="http://127.0.1:1234/v1", model="lfm3-1.7b",
            api_key="lm-studio", prefill="{", max_tokens=max_tokens,
            timeout_s=4.0, enabled=True,
        )
    )
    return calls


def _arm_disabled(monkeypatch, max_tokens: int = 128) -> list[dict]:
    """Arm the config as DISABLED; judge must never be called."""
    calls: list[dict] = []

    async def _judge(state, config, **kwargs):
        calls.append({"state": dict(state), "config": config})
        return {"action": "quiet", "think": False, "next_check_s": 300,
                "reason": "", "source": "reflex"}

    monkeypatch.setattr(server_mod._reflex_module, "judge", _judge)
    server_mod.set_reflex_config_override(
        ReflexConfig(
            url="http://127.0.1:1234/v1", model="lfm3-1.7b",
            api_key="lm-studio", prefill="{", max_tokens=max_tokens,
            timeout_s=4.0, enabled=False,
        )
    )
    return calls


def _quiet_store(tmp_path) -> DStateStore:
    """An ACTIVE D store (D-quiet needs it; see the vitals wiring tests)."""
    store = DStateStore(tmp_path / "dstate.json")
    store.summary = "上一轮总结"
    return store


def _seed_fast_thoughts(ledger, texts: list[str]) -> None:
    """Seed the ledger with fast entries (the reflex state reads these)."""
    for text in texts:
        ledger.record_fast(text)


# ── 0. Parity: TEMPO_RANGE_S mirrors _TEMPO_SPECS ────────────────────


def test_reflex_tempo_ranges_match_server_tempo_specs():
    assert reflex.TEMPO_RANGE_S == {
        mode: tuple(spec["interval_s"]) for mode, spec in server_mod._TEMPO_SPECS.items()
    }


# ── 1. Quiet reflex tick: short-circuit with small billing ──────────


def test_quiet_reflex_tick_short_circuits_and_bills_small(life, monkeypatch, tmp_path):
    vitals, ledger = life
    store = _quiet_store(tmp_path)
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)
    _seed_fast_thoughts(ledger, ["有点困", "好安静", "梦到海", "更早的念头"])
    calls = _arm(monkeypatch, {
        "action": "quiet", "think": False, "next_check_s": 300,
        "reason": "深夜安静，继续睡", "source": "reflex",
        "latency_ms": 12, "model": "lfm3-1.7b",
    })
    provider = _RecordingOpenAI([[{"type": "delta", "content": "不该出现"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive(action="quiet"))
    percent_before = vitals.percent()

    frames = _run(provider, _req(messages=_PROACTIVE))

    # SSE contract: start → tempo → next_check_s → quiet → inner → end,
    # no delta (the provider was never called).
    assert _events(frames) == [
        "start", "tempo", "next_check_s", "quiet", "inner", "end",
    ]
    assert _next_check_frames(frames) == [{"event": "next_check_s", "seconds": 300}]
    assert _visible(frames) == ""
    assert provider.calls == []
    assert len(calls) == 1
    # Reflex state the judge saw: D advisory folded into the summary tail,
    # tempo mode resolved, idle/mood present, no fabricated new events,
    # recent thoughts = the LEDGER's fast entries (≤3, chronological).
    state = calls[0]["state"]
    assert state["summary"].endswith("；D 建议：quiet")
    assert state["d_action"] == "quiet"
    assert state["tempo_mode"] == "slow"
    assert state["new_events"] == 0
    assert state["user_present"] is False
    assert state["recent_thoughts"] == ["好安静", "梦到海", "更早的念头"]
    assert state["mood"] == "平静"
    assert isinstance(state["idle_s"], (int, float))
    # Battery: ONE small drain of the documented constant
    # (300 prompt tokens + config.max_tokens completion) × 0.5 multiplier.
    expected_units = (300 + 128) * 0.5
    expected_percent = max(
        0.0, percent_before - expected_units / vitals.capacity * 100.0
    )
    assert vitals.percent() == pytest.approx(expected_percent, abs=1e-6)
    # Ledger: ONE fast entry added by this round ("<action> <reason>")
    # on top of the 4 seeded thoughts, plus one round tick.
    assert ledger.round_count() == 1
    counts = ledger.counts()
    assert counts["fast"] == 5
    assert counts["slow"] == 0
    entry = ledger.entries()[-1]
    assert entry["kind"] == "fast"
    assert entry["text"] == "quiet 深夜安静，继续睡"
    # drain_main NOT called (battery math above would not match otherwise).
    # Brain window: the quiet + round_end events carry the reflex reason.
    types = [e["type"] for e in brain_mod.snapshot()]
    assert "quiet" in types
    assert "round_end" in types
    quiet_evt = next(e for e in brain_mod.snapshot() if e["type"] == "quiet")
    assert quiet_evt["meta"]["reason"] == "reflex"
    assert quiet_evt["meta"]["generated"] is False


def test_quiet_reflex_tick_disabled_vitals_ledger_tolerated(life, monkeypatch, tmp_path):
    """No vitals/ledger singletons → the reflex-only round still completes."""
    server_mod.set_vitals(None)
    server_mod.set_ledger(None)
    store = _quiet_store(tmp_path)
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)
    _arm(monkeypatch, {
        "action": "quiet", "think": False, "next_check_s": 120,
        "reason": "", "source": "reflex",
    })
    provider = _RecordingOpenAI([[{"type": "delta", "content": "x"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive(action="quiet"))
    frames = _run(provider, _req(messages=_PROACTIVE))
    assert _events(frames) == [
        "start", "tempo", "next_check_s", "quiet", "inner", "end",
    ]
    assert _next_check_frames(frames) == [{"event": "next_check_s", "seconds": 120}]


# ── 2. Escalation: provider runs, finally bills the main round ──────


def test_reflex_escalation_runs_provider_and_bills_main(life, monkeypatch, tmp_path):
    vitals, ledger = life
    store = _quiet_store(tmp_path)
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)
    _arm(monkeypatch, {
        "action": "speak", "think": True, "next_check_s": 90,
        "reason": "想说话", "source": "reflex",
    })
    provider = _RecordingOpenAI([[
        {"type": "delta", "content": "（升级了，我来说一句。）"},
        {"type": "end"},
    ]])
    _install(monkeypatch, provider, passive=_passive(action="quiet"))
    percent_before = vitals.percent()

    frames = _run(provider, _req(messages=_PROACTIVE))

    assert frames[-1]["event"] == "end"
    # The provider WAS called and its text reached the stream.
    assert len(provider.calls) == 1
    assert "升级了" in _visible(frames)
    # next_check_s is announced once, right before the final end frame.
    assert _next_check_frames(frames) == [{"event": "next_check_s", "seconds": 90}]
    assert _events(frames)[-2:] == ["next_check_s", "end"]
    # The existing finally drained the MAIN battery and ticked the ledger.
    assert vitals.percent() < percent_before
    assert ledger.round_count() == 1
    assert ledger.counts()["slow"] == 1
    assert ledger.counts()["fast"] == 0
    # D bookkeeping was recorded for the round (record_round + save ran).
    assert store.last_round.get("round_id")
    # No reflex-only fast entry / small drain on the escalation path.
    entry = ledger.entries()[-1]
    assert entry["kind"] == "slow"


def test_reflex_nonquiet_action_escalates_without_think(life, monkeypatch, tmp_path):
    vitals, ledger = life
    store = _quiet_store(tmp_path)
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)
    _arm(monkeypatch, {
        "action": "look", "think": False, "next_check_s": 70,
        "reason": "看一眼周围", "source": "reflex",
    })
    provider = _RecordingOpenAI([[
        {"type": "delta", "content": "[INNER:瞄了一眼窗台]"},
        {"type": "end"},
    ]])
    _install(monkeypatch, provider, passive=_passive(action="quiet"))
    percent_before = vitals.percent()
    frames = _run(provider, _req(messages=_PROACTIVE))
    assert frames[-1]["event"] == "end"
    assert len(provider.calls) == 1, "non-quiet action wakes the brain even with think=false"
    assert _next_check_frames(frames) == [{"event": "next_check_s", "seconds": 70}]
    assert ledger.round_count() == 1
    assert vitals.percent() < percent_before


# ── 3. D-quiet vs reflex-escalate: reflex veto beats D ──────────────


def test_reflex_veto_overrides_d_quiet(life, monkeypatch, tmp_path):
    vitals, ledger = life
    store = _quiet_store(tmp_path)
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)
    _arm(monkeypatch, {
        "action": "speak", "think": True, "next_check_s": 90,
        "reason": "直觉想说话", "source": "reflex",
    })
    provider = _RecordingOpenAI([[
        {"type": "delta", "content": "嗯？怎么了。"},
        {"type": "end"},
    ]])
    _install(monkeypatch, provider, passive=_passive(action="quiet"))
    frames = _run(provider, _req(messages=_PROACTIVE))
    assert frames[-1]["event"] == "end"
    assert len(provider.calls) == 1, "reflex escalation vetoes D-quiet"
    assert _next_check_frames(frames) == [{"event": "next_check_s", "seconds": 90}]
    # D bookkeeping was still recorded (the veto does not lose the round).
    assert store.last_round.get("round_id")


# ── 4. D non-quiet + reflex quiet: the gate still short-circuits ────


def test_d_speak_with_reflex_quiet_judge_short_circuits(life, monkeypatch, tmp_path):
    """Full gate coverage: the reflex judge vets EVERY proactive round.
    D says speak but the reflex body keeps the tick quiet → no provider."""
    vitals, ledger = life
    store = _quiet_store(tmp_path)
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)
    calls = _arm(monkeypatch, {
        "action": "quiet", "think": False, "next_check_s": 300,
        "reason": "身体自己待着就好", "source": "reflex",
    })
    provider = _RecordingOpenAI([[
        {"type": "delta", "content": "不该出现"},
        {"type": "end"},
    ]])
    _install(monkeypatch, provider, passive=_passive())
    percent_before = vitals.percent()
    frames = _run(provider, _req(messages=_PROACTIVE))
    # Reflex-only round shape, exactly like the D-quiet quiet tick.
    assert _events(frames) == [
        "start", "tempo", "next_check_s", "quiet", "inner", "end",
    ]
    assert provider.calls == []
    assert len(calls) == 1
    assert calls[0]["state"]["d_action"] == "speak"
    assert calls[0]["state"]["summary"].endswith("；D 建议：speak")
    expected_percent = max(
        0.0, percent_before - (300 + 128) * 0.5 / vitals.capacity * 100.0
    )
    assert vitals.percent() == pytest.approx(expected_percent, abs=1e-6)
    assert ledger.round_count() == 1
    counts = ledger.counts()
    assert counts["fast"] == 1
    assert counts["slow"] == 0


# ── 5. Disabled config: old D-quiet early return, verbatim ──────────


def test_disabled_reflex_preserves_old_d_quiet_behavior(life, monkeypatch, tmp_path):
    vitals, ledger = life
    store = _quiet_store(tmp_path)
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)
    calls = _arm_disabled(monkeypatch)
    provider = _RecordingOpenAI([[{"type": "delta", "content": "不该出现"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive(action="quiet"))
    percent_before = vitals.percent()

    frames = _run(provider, _req(messages=_PROACTIVE))

    # Old world SSE shape: no next_check_s, no quiet frame, plain inner.
    assert _events(frames) == ["start", "tempo", "inner", "end"]
    assert _next_check_frames(frames) == []
    assert provider.calls == []
    assert calls == [], "disabled config must never call judge"
    # Old world billing: no drain_small, no record_fast, no round_tick.
    assert vitals.percent() == pytest.approx(percent_before, abs=1e-9)
    assert ledger.round_count() == 0
    assert ledger.entries() == []
    assert telemetry_mod._instance.counters.get("reflex.ticks", 0.0) == 0.0
    assert telemetry_mod._instance.counters.get("reflex.fallback", 0.0) == 0.0


def test_absent_reflex_config_preserves_old_d_quiet_behavior(life, monkeypatch, tmp_path):
    """No override + no boot singleton → reflex layer absent → old world."""
    vitals, ledger = life
    store = _quiet_store(tmp_path)
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)
    judge_calls: list[dict] = []

    async def _judge(state, config, **kwargs):
        judge_calls.append({"state": dict(state), "config": config})
        return {"action": "quiet", "think": False, "next_check_s": 300,
                "reason": "", "source": "reflex"}

    monkeypatch.setattr(server_mod._reflex_module, "judge", _judge)
    provider = _RecordingOpenAI([[{"type": "delta", "content": "不该出现"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive(action="quiet"))
    percent_before = vitals.percent()
    frames = _run(provider, _req(messages=_PROACTIVE))
    assert _events(frames) == ["start", "tempo", "inner", "end"]
    assert provider.calls == []
    assert judge_calls == []
    assert vitals.percent() == pytest.approx(percent_before, abs=1e-9)
    assert ledger.round_count() == 0


def test_absent_reflex_config_d_nonquiet_runs_provider(life, monkeypatch, tmp_path):
    """No reflex layer + D says speak → the main brain runs as today (the
    gate never vetoes when the layer is off)."""
    vitals, ledger = life
    store = _quiet_store(tmp_path)
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)
    judge_calls: list[dict] = []

    async def _judge(state, config, **kwargs):
        judge_calls.append({"state": dict(state), "config": config})
        return {"action": "quiet", "think": False, "next_check_s": 300,
                "reason": "", "source": "reflex"}

    monkeypatch.setattr(server_mod._reflex_module, "judge", _judge)
    provider = _RecordingOpenAI([[
        {"type": "delta", "content": "我说一句。"},
        {"type": "end"},
    ]])
    _install(monkeypatch, provider, passive=_passive())
    frames = _run(provider, _req(messages=_PROACTIVE))
    assert frames[-1]["event"] == "end"
    assert len(provider.calls) == 1
    assert _visible(frames) == "我说一句。"
    assert judge_calls == []
    assert _next_check_frames(frames) == []
    assert ledger.round_count() == 1
    assert ledger.counts()["slow"] == 1


# ── 6. Fallback: same shape as quiet + telemetry counter ────────────


def test_fallback_quiet_treated_like_reflex_quiet(life, monkeypatch, tmp_path):
    vitals, ledger = life
    store = _quiet_store(tmp_path)
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)
    _arm(monkeypatch, {
        "action": "quiet", "think": False, "next_check_s": 300,
        "reason": "", "source": "fallback", "failure": "timeout",
    })
    provider = _RecordingOpenAI([[{"type": "delta", "content": "不该出现"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive(action="quiet"))
    percent_before = vitals.percent()
    frames = _run(provider, _req(messages=_PROACTIVE))
    # Safe default: exactly the reflex-only round shape.
    assert _events(frames) == [
        "start", "tempo", "next_check_s", "quiet", "inner", "end",
    ]
    assert provider.calls == []
    assert vitals.percent() == pytest.approx(
        max(0.0, percent_before - (300 + 128) * 0.5 / vitals.capacity * 100.0),
        abs=1e-6,
    )
    assert ledger.round_count() == 1
    counts = ledger.counts()
    assert counts["fast"] == 1
    assert counts["slow"] == 0
    # Fallback fast entry: empty reason → bare "<action>".
    entry = ledger.entries()[-1]
    assert entry["text"] == "quiet"
    # Telemetry: the fallback counter fired alongside the tick counter.
    assert telemetry_mod._instance.counters.get("reflex.ticks", 0.0) == 1.0
    assert telemetry_mod._instance.counters.get("reflex.fallback", 0.0) == 1.0


# ── 7. Asleep skip: judge never consulted ───────────────────────────


def test_asleep_proactive_skips_reflex_gate(life, monkeypatch, tmp_path):
    vitals, ledger = life
    vitals.start_sleep(3600.0)
    assert vitals.is_asleep()
    store = _quiet_store(tmp_path)
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)
    judge_calls: list[dict] = []

    async def _judge(state, config, **kwargs):
        judge_calls.append({"state": dict(state), "config": config})
        return {"action": "quiet", "think": False, "next_check_s": 300,
                "reason": "", "source": "reflex"}

    monkeypatch.setattr(server_mod._reflex_module, "judge", _judge)
    provider = _RecordingOpenAI([[{"type": "delta", "content": "不该出现"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive(action="quiet"))
    frames = _run(provider, _req(messages=_PROACTIVE))
    # Pre-D vitals short-circuit keeps its machine-event shape; the
    # reflex gate is never reached.
    assert _events(frames) == ["start", "sleep_state", "end"]
    assert frames[1] == {"event": "sleep_state", "state": "asleep"}
    assert _visible(frames) == ""
    assert provider.calls == []
    assert judge_calls == []
    assert ledger.round_count() == 0


# ── 8. User turns are never gated ───────────────────────────────────


def test_user_turn_never_reaches_reflex_gate(life, monkeypatch, tmp_path):
    vitals, ledger = life
    store = _quiet_store(tmp_path)
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)
    # Even with the reflex layer ARMED and its judge returning quiet, a
    # real user turn is never gated: the answer goes out normally.
    calls = _arm(monkeypatch, {
        "action": "quiet", "think": False, "next_check_s": 300,
        "reason": "", "source": "reflex",
    })
    provider = _RecordingOpenAI([[
        {"type": "delta", "content": "我在呢。"},
        {"type": "end"},
    ]])
    _install(monkeypatch, provider, passive=_passive(action="quiet"))
    frames = _run(provider, _req())
    assert frames[-1]["event"] == "end"
    assert len(provider.calls) == 1
    assert _visible(frames) == "我在呢。"
    assert _next_check_frames(frames) == []
    assert calls == [], "user turns are never gated by the reflex"
    assert ledger.round_count() == 1
    assert ledger.counts()["fast"] == 0


# ── 9. Emitted next_check_s stays inside the tempo tier ─────────────


@pytest.mark.parametrize("mode,lo,hi", [
    ("slow", 240, 360), ("fast", 90, 150), ("ultra", 50, 70),
])
def test_emitted_next_check_s_stays_inside_tier(life, monkeypatch, tmp_path, mode, lo, hi):
    """Whatever judge returns, the emitted seconds are the tier-clamped
    value (reflex._validate_decision clamps; wiring passes it through)."""
    vitals, _ledger = life
    store = _quiet_store(tmp_path)
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)
    _arm(monkeypatch, {
        "action": "quiet", "think": False, "next_check_s": lo,
        "reason": "整点自查", "source": "reflex",
    })
    provider = _RecordingOpenAI([[{"type": "delta", "content": "x"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive(action="quiet"))

    # Force the tempo mode so the reflex state's tempo_mode is `mode`.
    server_mod._set_tempo(mode, "test")

    frames = _run(provider, _req(messages=_PROACTIVE))
    checks = _next_check_frames(frames)
    assert len(checks) == 1
    seconds = checks[0]["seconds"]
    assert lo <= seconds <= hi
    assert checks[0] == {"event": "next_check_s", "seconds": lo}
    # The tempo SSE frame carries the same mode the state saw.
    tempo_frames = [f for f in frames if f.get("event") == "tempo"]
    assert tempo_frames and tempo_frames[0]["mode"] == mode


# ── 10. Reflex is the SOLE decision maker: budget reaches the provider ──


def test_reflex_override_reaches_provider_budget_and_prompt(life, monkeypatch, tmp_path):
    """The small model's think/reaction must reach the provider.

    Regression: the override used to run after ``req.thinking`` /
    ``_d_budget`` were already assigned from D's prior sampling, so the
    reflex budget was written to the log only — the provider kept running on
    prior D. The window here returns large+think while D's prior says
    small+no_thinking.
    """
    vitals, ledger = life
    store = _quiet_store(tmp_path)
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)
    _arm(monkeypatch, {
        "action": "speak", "think": True, "next_check_s": 90,
        "reaction": "large", "reason": "想说长一点", "source": "reflex",
        "time_relevant": False, "time_note": "",
    })
    provider = _RecordingOpenAI([[
        {"type": "delta", "content": "（这次要好好说。）"},
        {"type": "end"},
    ]])
    _install(monkeypatch, provider, passive=_passive(
        action="quiet", strength="no_thinking", reaction="small", max_tokens=2048,
    ))

    frames = _run(provider, _req(messages=_PROACTIVE))

    assert frames[-1]["event"] == "end"
    assert len(provider.calls) == 1
    call = provider.calls[0]
    # Provider-side budget is the reflex decision, not D's prior.
    assert call["max_tokens"] == cortex.REACTION_MAX_TOKENS["large"]
    assert call["thinking_override"] is True
    assert call["reasoning_effort_override"] == "low"
    # The prompt the main brain actually reads also carries reflex's text.
    assert "思考强度：low" in call["system"]
    assert "持续反应：大" in call["system"]
    assert f"max_tokens={cortex.REACTION_MAX_TOKENS['large']}" in call["system"]
    # Brain panel: ONE d_decision, already the reflex-overridden one.
    d_events = [e for e in brain_mod.snapshot() if e["type"] == "d_decision"]
    assert len(d_events) == 1
    meta = d_events[0]["meta"]
    assert meta["source"] == "reflex"
    assert meta["strength"] == "low"
    assert meta["reaction"] == "large"
    assert meta["max_tokens"] == cortex.REACTION_MAX_TOKENS["large"]
    # Reflex answers are discrete judgments, not sampled posteriors: no chip.
    assert meta.get("confidence") is None
    # 面板"延迟"报的是真正裁决者（小模型）的整拍耗时，不是 D 那条已经
    # 没用的 Laya 尝试的耗时（否则会显示成 2 秒的假延迟）。
    assert meta["latency_ms"] < 1000.0


def test_reflex_quiet_publishes_reflex_decision_to_brain(life, monkeypatch, tmp_path):
    """The quiet path publishes the final decision too — no stale prior line."""
    vitals, ledger = life
    store = _quiet_store(tmp_path)
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)
    _arm(monkeypatch, {
        "action": "quiet", "think": False, "next_check_s": 300,
        "reason": "继续待着", "source": "reflex",
        "time_relevant": False, "time_note": "",
    })
    provider = _RecordingOpenAI([[
        {"type": "delta", "content": "不该出现"},
        {"type": "end"},
    ]])
    _install(monkeypatch, provider, passive=_passive(
        action="speak", strength="no_thinking", reaction="small",
    ))
    frames = _run(provider, _req(messages=_PROACTIVE))
    assert _events(frames) == [
        "start", "tempo", "next_check_s", "quiet", "inner", "end",
    ]
    d_events = [e for e in brain_mod.snapshot() if e["type"] == "d_decision"]
    assert len(d_events) == 1
    meta = d_events[0]["meta"]
    assert meta["source"] == "reflex"
    assert meta["action"] == "quiet"
    assert meta.get("confidence") is None


def test_reflex_fallback_is_labelled_distinctly(life, monkeypatch, tmp_path):
    """小模型没答上时必须一眼看出：来源标 reflex·回落，而不是 reflex。

    生产环境曾出现"每拍都超时回落"，面板却显示 来源 reflex——看起来像
    小模型在决策，实际它一个字都没答。
    """
    vitals, ledger = life
    store = _quiet_store(tmp_path)
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)
    _arm(monkeypatch, {
        "action": "quiet", "think": False, "next_check_s": 300,
        "reason": "", "source": "fallback", "failure": "timeout",
    })
    provider = _RecordingOpenAI([[
        {"type": "delta", "content": "不该出现"},
        {"type": "end"},
    ]])
    _install(monkeypatch, provider, passive=_passive(action="speak"))
    _run(provider, _req(messages=_PROACTIVE))
    d_events = [e for e in brain_mod.snapshot() if e["type"] == "d_decision"]
    assert len(d_events) == 1
    assert d_events[0]["meta"]["source"] == "reflex·回落"


# ── 11. Model-decided time sense: closed loop across ticks ──────────


def _scripted_arm(monkeypatch, judged_sequence: list[dict]) -> list[dict]:
    calls: list[dict] = []
    pending = list(judged_sequence)

    async def _judge(state, config, **kwargs):
        calls.append({"state": dict(state), "config": config})
        if pending:
            return dict(pending.pop(0))
        return {
            "action": "quiet", "think": False, "next_check_s": 300,
            "reason": "", "source": "reflex",
            "time_relevant": False, "time_note": "",
        }

    monkeypatch.setattr(
        server_mod._reflex_module, "judge", _judge, raising=False
    )
    server_mod.set_reflex_config_override(
        ReflexConfig(
            url="http://127.0.1:1234/v1", model="lfm3-1.7b",
            api_key="lm-studio", prefill="{", max_tokens=128,
            timeout_s=4.0, enabled=True,
        )
    )
    return calls


def test_time_sense_is_model_requested_and_granted_next_tick(life, monkeypatch, tmp_path):
    """The clock is never pushed: tick N asks, tick N+1 receives.

    Regression: the grant read the per-round ``_reflex_decision`` local that
    was reset to None before the check, so ``time_relevant=true`` never
    unlocked anything.
    """
    vitals, ledger = life
    store = _quiet_store(tmp_path)
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)
    calls = _scripted_arm(monkeypatch, [
        {"action": "quiet", "think": False, "next_check_s": 300,
         "reason": "想知道现在几点", "source": "reflex",
         "time_relevant": True, "time_note": "上一轮大概是 20 分钟前"},
        {"action": "quiet", "think": False, "next_check_s": 300,
         "reason": "拿到时间了", "source": "reflex",
         "time_relevant": False, "time_note": ""},
    ])
    provider = _RecordingOpenAI([[{"type": "delta", "content": "x"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive(action="quiet"))

    # Tick 1: the model asks. It gets NO clock (nothing was requested before).
    _run(provider, _req(messages=_PROACTIVE))
    assert "current_time" not in calls[0]["state"]
    assert "elapsed_since_main_s" not in calls[0]["state"]

    # A main-brain round happened 11 minutes ago (elapsed_since_main_s anchor).
    server_mod._DENSITY_STATE["last_round_at"] = time.time() - 660.0

    # Tick 2: the request from tick 1 is honored — clock + elapsed are attached.
    _run(provider, _req(messages=_PROACTIVE))
    assert re.fullmatch(r"\d{2}:\d{2}", calls[1]["state"]["current_time"])
    assert calls[1]["state"]["elapsed_since_main_s"] >= 600

    # Tick 3: the model said false on tick 2, so the clock is gone again.
    _run(provider, _req(messages=_PROACTIVE))
    assert "current_time" not in calls[2]["state"]
    assert "elapsed_since_main_s" not in calls[2]["state"]


def test_time_sense_latch_survives_fallback(life, monkeypatch, tmp_path):
    """A fallback tick also persists the (false) time decision — no ghost grant."""
    vitals, ledger = life
    store = _quiet_store(tmp_path)
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)
    calls = _scripted_arm(monkeypatch, [
        {"action": "quiet", "think": False, "next_check_s": 300,
         "reason": "", "source": "fallback", "failure": "timeout"},
    ])
    provider = _RecordingOpenAI([[{"type": "delta", "content": "x"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive(action="quiet"))
    _run(provider, _req(messages=_PROACTIVE))
    assert server_mod._REFLEX_TIME_STATE["wanted"] is False
    _run(provider, _req(messages=_PROACTIVE))
    assert "current_time" not in calls[1]["state"]
