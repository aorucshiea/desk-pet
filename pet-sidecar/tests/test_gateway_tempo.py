"""The tempo (节奏) state machine: three cadence tiers + chat-mode entry.

Design (2026-11 tempo rework):
- Modes are exactly ultra/fast/slow; the resting cadence is "slow"
  (~4–6 min). Idle no longer scales tempo — a separate battery/fatigue
  system will own rest; slowing the main-brain cadence IS the token saving.
- A server-side store (_TEMPO_STATE) holds a model-set (pet_set_tempo) or
  chat-entry tempo for _TEMPO_TTL_S seconds; a fresh entry beats derivation,
  an expired/empty store falls back to the resting "slow".
- A real user turn enters chat mode: "fast", upgrading to "ultra" when the
  previous real user turn is within 120 s (active conversation).
- Temperature composition: tempo base + emotion modulation + attention
  density nudge (±0.01), clamped; an explicitly requested temperature
  (≠ the ChatRequest default) is respected verbatim.

These tests drive the real ``_stream_chat_provider`` with a scripted
provider (same harness shape as test_gateway_cortex.py) plus direct unit
tests for the deterministic density rules.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest

from gateway import brain as brain_mod
from gateway import server as server_mod
from gateway.decision import cortex
from gateway.decision.cortex import DStateStore
from gateway.providers.openai import OpenAIProvider
from gateway.providers.base import ToolDef
from gateway.server import ChatRequest


# ── Stubs (same shape as test_gateway_cortex.py) ────────────────────


@pytest.fixture(autouse=True)
def _isolate_life_state(monkeypatch):
    monkeypatch.setattr(server_mod, "_dstate_provider", None)
    monkeypatch.setattr(server_mod, "_body_log_ref", None)
    monkeypatch.setattr(server_mod, "_ephemeral_ref", None)
    monkeypatch.setattr(server_mod, "_last_user_msg", "")
    monkeypatch.setattr(server_mod, "_last_user_ts", 0.0)
    monkeypatch.setattr(server_mod, "_last_real_user_ts", 0.0)
    # Fresh tempo + density state per test.
    server_mod._TEMPO_STATE.clear()
    server_mod._TEMPO_STATE.update(
        {"mode": "", "expires_at": 0.0, "source": ""}
    )
    server_mod._DENSITY_STATE.clear()
    server_mod._DENSITY_STATE.update(
        {"last_round_at": 0.0, "last_user_turn_at": 0.0}
    )
    server_mod._DENSITY_EVENTS.clear()

    async def _no_body(*_a, **_k):
        return {"status": "not_applied", "applied": False}

    monkeypatch.setattr(server_mod, "_pet_action", _no_body)
    monkeypatch.setattr(server_mod, "_format_vision_context", lambda: "")


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


class _ResultMCP:
    def __init__(self, result) -> None:
        self.result = result
        self.called: list[tuple] = []

    async def call_tool(self, server_name, tool_name, arguments):
        self.called.append((server_name, tool_name, arguments))
        return self.result


class _StubBridge:
    def new_session(self) -> None: ...

    def post(self, *_a, **_k) -> None: ...


class _StubMood:
    emotion_index = 0.1

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
        "thinking": True,
        "reasoning_effort": "low",
        "strength": "low",
        "reaction": "small",
        "max_tokens": 2048,
        "action": "speak",
        "action_label": "说",
        "source": "test",
        "confidence": 0.75,
        "reason": "test|thinking=low,reaction=small,action=speak",
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


def _run_with_mcp(provider, req, mcp) -> list[dict]:
    async def _drain():
        frames: list[dict] = []
        async for chunk in server_mod._stream_chat_provider(
            _StubRegistry(provider),
            mcp,
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


def _use_store(monkeypatch, tmp_path) -> DStateStore:
    store = DStateStore(Path(tmp_path) / "dstate.json")
    store.summary = "上一轮总结：用户在调试网关"
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)
    return store


def _tempo_frames(frames: list[dict]) -> list[dict]:
    return [f for f in frames if f.get("event") == "tempo"]


def _expire_tempo_store() -> None:
    server_mod._TEMPO_STATE["expires_at"] = time.time() - 1.0


# ── Mode table contract ─────────────────────────────────────────────


def test_mode_table_is_exactly_three_tiers():
    assert server_mod._TEMPO_MODES == ("ultra", "fast", "slow")
    assert set(server_mod._TEMPO_SPECS) == {"ultra", "fast", "slow"}
    assert server_mod._TEMPO_SPECS["ultra"]["interval_s"] == (50, 70)
    assert server_mod._TEMPO_SPECS["fast"]["interval_s"] == (90, 150)
    assert server_mod._TEMPO_SPECS["slow"]["interval_s"] == (240, 360)
    assert server_mod._TEMPO_SPECS["ultra"]["temp"] == 1.3
    assert server_mod._TEMPO_SPECS["fast"]["temp"] == 1.2
    assert server_mod._TEMPO_SPECS["slow"]["temp"] == 1.0
    assert server_mod._TEMPO_DEFAULT == "slow"
    assert server_mod._TEMPO_TTL_S == 600.0
    # No renderer-visible legacy modes anywhere in the tool schema.
    assert "standard" not in server_mod._TEMPO_MODES
    assert "daze" not in server_mod._TEMPO_MODES
    assert "asleep" not in server_mod._TEMPO_MODES


# ── Resolution precedence ───────────────────────────────────────────


def test_empty_store_derives_the_resting_slow():
    assert server_mod._resolve_tempo(time.time()) == ("slow", "derive")


def test_store_active_beats_derivation():
    server_mod._set_tempo("ultra", "model")
    mode, source = server_mod._resolve_tempo(time.time() + 1)
    assert (mode, source) == ("ultra", "model")
    server_mod._set_tempo("fast", "chat")
    mode, source = server_mod._resolve_tempo(time.time() + 1)
    assert (mode, source) == ("fast", "chat")


def test_expired_store_falls_back_to_derivation():
    server_mod._set_tempo("ultra", "model")
    _expire_tempo_store()
    assert server_mod._resolve_tempo(time.time()) == ("slow", "derive")


def test_set_tempo_invalid_mode_falls_back_to_slow():
    assert server_mod._set_tempo("daze", "model") == "slow"
    assert server_mod._TEMPO_STATE["mode"] == "slow"
    assert server_mod._TEMPO_STATE["source"] == "model"
    assert server_mod._TEMPO_STATE["expires_at"] > time.time() + 500


def test_set_tempo_expires_after_ttl():
    now = time.time()
    server_mod._set_tempo("fast", "model", ttl_s=1.0)
    assert server_mod._TEMPO_STATE["expires_at"] == pytest.approx(now + 1.0)
    assert server_mod._resolve_tempo(now + 0.5)[0] == "fast"
    assert server_mod._resolve_tempo(now + 1.5) == ("slow", "derive")


# ── Derivation: idle no longer steers tempo ─────────────────────────


def test_proactive_quiet_round_keeps_slow_regardless_of_idle(monkeypatch, tmp_path):
    """The old idle ladder (1800s→asleep, 600s→daze) is gone: even a huge
    idle on a quiet life round rests at the default slow cadence."""
    _use_store(monkeypatch, tmp_path)
    monkeypatch.setattr(
        server_mod, "_last_real_user_ts", time.time() - 4000.0
    )

    async def _passive(_store, state=None, **_kw):
        return _decision(action="quiet")

    provider = _RecordingOpenAI([[{"type": "delta", "content": "困"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)

    frames = _run(provider, _req(messages=_PROACTIVE))
    tempo = _tempo_frames(frames)
    assert len(tempo) == 1
    assert tempo[0]["mode"] == "slow"
    assert server_mod._TEMPO_STATE["mode"] in ("", "slow") or (
        server_mod._TEMPO_STATE["expires_at"] <= time.time()
    )


def test_proactive_speak_round_derives_slow(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        return _decision(action="speak")

    provider = _RecordingOpenAI([[{"type": "delta", "content": "嗯。"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)

    frames = _run(provider, _req(messages=_PROACTIVE))
    tempo = _tempo_frames(frames)
    assert len(tempo) == 1
    assert tempo[0]["mode"] == "slow"


# ── Chat-mode entry ─────────────────────────────────────────────────


def test_real_user_turn_sets_fast_chat_tempo(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        return _decision()

    provider = _RecordingOpenAI([[{"type": "delta", "content": "在呢"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)

    frames = _run(provider, _req())

    tempo = _tempo_frames(frames)
    assert len(tempo) == 1
    assert tempo[0]["mode"] == "fast"
    assert server_mod._TEMPO_STATE["mode"] == "fast"
    assert server_mod._TEMPO_STATE["source"] == "chat"
    assert server_mod._TEMPO_STATE["expires_at"] > time.time() + 500


def test_second_user_turn_within_120s_upgrades_to_ultra(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        return _decision()

    provider = _RecordingOpenAI([[{"type": "delta", "content": "嗯"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)

    _run(provider, _req(messages=[{"role": "user", "content": "第一句"}]))
    assert server_mod._TEMPO_STATE["mode"] == "fast"

    # Second real user turn moments later (≪ 120 s real gap in a test).
    frames = _run(provider, _req(messages=[{"role": "user", "content": "第二句"}]))
    tempo = _tempo_frames(frames)
    assert tempo and tempo[-1]["mode"] == "ultra"
    assert server_mod._TEMPO_STATE["mode"] == "ultra"
    assert server_mod._TEMPO_STATE["source"] == "chat"


def test_ultra_refreshes_ttl_on_conversation(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        return _decision()

    provider = _RecordingOpenAI([[{"type": "delta", "content": "嗯"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)

    _run(provider, _req(messages=[{"role": "user", "content": "一"}]))
    _run(provider, _req(messages=[{"role": "user", "content": "二"}]))
    assert server_mod._TEMPO_STATE["mode"] == "ultra"
    first_expiry = server_mod._TEMPO_STATE["expires_at"]
    # A third turn refreshes the TTL window (still ultra).
    _run(provider, _req(messages=[{"role": "user", "content": "三"}]))
    assert server_mod._TEMPO_STATE["mode"] == "ultra"
    assert server_mod._TEMPO_STATE["expires_at"] >= first_expiry


def test_chat_tempo_expires_back_into_derivation(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        return _decision()

    provider = _RecordingOpenAI([[{"type": "delta", "content": "嗯"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)

    _run(provider, _req())
    assert server_mod._TEMPO_STATE["mode"] == "fast"
    _expire_tempo_store()

    frames = _run(provider, _req(messages=_PROACTIVE))
    tempo = _tempo_frames(frames)
    assert tempo and tempo[0]["mode"] == "slow"


def test_chat_entry_also_holds_without_a_d_store(monkeypatch):
    async def _passive(*_a, **_kw):  # pragma: no cover - must not be called
        raise AssertionError("no D store ⇒ no cortex call")

    provider = _RecordingOpenAI([[{"type": "delta", "content": "嗯"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)
    monkeypatch.setattr(server_mod, "_dstate_provider", None)

    frames = _run(provider, _req())
    assert _tempo_frames(frames) and _tempo_frames(frames)[0]["mode"] == "fast"
    assert server_mod._TEMPO_STATE["mode"] == "fast"


def test_silent_round_emits_no_tempo_and_keeps_request_temperature(monkeypatch):
    provider = _RecordingOpenAI([[{"type": "delta", "content": "narration"}, {"type": "end"}]])
    _install(monkeypatch, provider)

    _run(provider, _req(silent=True))

    assert provider.calls[0]["temperature"] == pytest.approx(0.6)
    assert server_mod._TEMPO_STATE["mode"] == ""


# ── Temperature composition ─────────────────────────────────────────


def test_slow_tempo_base_applies_with_emotion(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)
    # Density "low": no user turn, no fresh events → +0.0.
    # slow base 1.0 + emotion 0.1*0.3 = 1.03.
    async def _passive(_store, state=None, **_kw):
        return _decision()

    provider = _RecordingOpenAI([[{"type": "delta", "content": "嗯"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)

    frames = _run(provider, _req(messages=_PROACTIVE))
    assert _tempo_frames(frames)[0]["mode"] == "slow"
    assert provider.calls[0]["temperature"] == pytest.approx(1.03)


def test_chat_turn_applies_fast_base_and_high_density(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        return _decision()

    provider = _RecordingOpenAI([[{"type": "delta", "content": "嗯"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)

    frames = _run(provider, _req())
    assert _tempo_frames(frames)[0]["mode"] == "fast"
    # fast base 1.2 + emotion 0.1*0.3 − high-density 0.01 = 1.22.
    assert provider.calls[0]["temperature"] == pytest.approx(1.22)


def test_ultra_temp_base_applies_in_active_conversation(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        return _decision()

    provider = _RecordingOpenAI([[{"type": "delta", "content": "嗯"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)

    _run(provider, _req(messages=[{"role": "user", "content": "一"}]))
    frames = _run(provider, _req(messages=[{"role": "user", "content": "二"}]))
    assert _tempo_frames(frames)[-1]["mode"] == "ultra"
    # ultra base 1.3 + emotion 0.1*0.3 − high-density 0.01 = 1.32.
    assert provider.calls[-1]["temperature"] == pytest.approx(1.32)


def test_temperature_clamps_at_temp_max(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)

    class _HotMood(_StubMood):
        emotion_index = 5.0

    async def _passive(_store, state=None, **_kw):
        return _decision()

    provider = _RecordingOpenAI([[{"type": "delta", "content": "嗯"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)

    async def _drain():
        frames: list[dict] = []
        async for chunk in server_mod._stream_chat_provider(
            _StubRegistry(provider), _StubMCP(), _StubBridge(),
            _req(messages=_PROACTIVE), server=None, server_state={},
            mood_store=_HotMood(), lora_arr=None,
        ):
            text = chunk.decode("utf-8")
            if text.startswith("data: "):
                frames.append(json.loads(text[6:]))
        return frames

    frames = asyncio.run(_drain())
    assert _tempo_frames(frames)[0]["mode"] == "slow"
    # 1.0 + 5.0*0.3 = 2.5 → clamped to TEMP_MAX 1.5.
    assert provider.calls[0]["temperature"] == pytest.approx(server_mod.TEMP_MAX)


def test_explicit_request_temperature_is_respected_verbatim(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        return _decision()

    provider = _RecordingOpenAI([[{"type": "delta", "content": "嗯"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)

    _run(provider, _req(temperature=0.25))
    assert provider.calls[0]["temperature"] == pytest.approx(0.25)


def test_default_request_temperature_is_not_explicit(monkeypatch, tmp_path):
    """temperature=0.6 (the ChatRequest default) means 'not set': the tempo
    base replaces it rather than composing on top of it."""
    _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        return _decision()

    provider = _RecordingOpenAI([[{"type": "delta", "content": "嗯"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)

    _run(provider, _req(messages=_PROACTIVE))
    assert provider.calls[0]["temperature"] != pytest.approx(0.6)
    assert provider.calls[0]["temperature"] == pytest.approx(1.03)


# ── Density rules (deterministic) ───────────────────────────────────


def test_density_level_high_only_for_recent_user_turn():
    server_mod._DENSITY_STATE["last_user_turn_at"] = 1000.0
    assert server_mod._density_level(1000.0 + 59.9) == "high"
    assert server_mod._density_level(1000.0 + 60.0) == "high"
    assert server_mod._density_level(1000.0 + 60.1) != "high"


def test_density_level_medium_for_user_turn_within_300s():
    server_mod._DENSITY_STATE["last_user_turn_at"] = 1000.0
    assert server_mod._density_level(1000.0 + 299.0) == "medium"
    assert server_mod._density_level(1000.0 + 300.0) == "medium"
    assert server_mod._density_level(1000.0 + 301.0) == "low"


def test_density_level_medium_for_two_fresh_events_since_last_round():
    server_mod._DENSITY_STATE["last_round_at"] = 1000.0
    server_mod._DENSITY_STATE["last_user_turn_at"] = 0.0
    server_mod._DENSITY_EVENTS.extend([(1010.0, "walk"), (1020.0, "turn")])
    assert server_mod._density_level(1025.0) == "medium"
    # One event is not enough.
    server_mod._DENSITY_EVENTS.clear()
    server_mod._DENSITY_EVENTS.append((1010.0, "walk"))
    assert server_mod._density_level(1025.0) == "low"


def test_density_events_only_count_within_the_round_and_window():
    server_mod._DENSITY_STATE["last_round_at"] = 1000.0
    server_mod._DENSITY_STATE["last_user_turn_at"] = 0.0
    # Both events predate the last round → stale.
    server_mod._DENSITY_EVENTS.extend([(500.0, "walk"), (600.0, "turn")])
    assert server_mod._density_level(1025.0) == "low"
    # Both events are ancient (outside the 300 s window) → stale.
    server_mod._DENSITY_STATE["last_round_at"] = 0.0
    assert server_mod._density_level(1025.0) == "low"


def test_density_delta_maps_levels_to_plusminus():
    server_mod._DENSITY_STATE["last_user_turn_at"] = 0.0
    server_mod._DENSITY_STATE["last_round_at"] = 0.0
    assert server_mod._density_delta(500.0) == pytest.approx(0.0)  # low
    server_mod._DENSITY_STATE["last_user_turn_at"] = 480.0
    assert server_mod._density_delta(500.0) == pytest.approx(-0.01)  # high
    server_mod._DENSITY_STATE["last_user_turn_at"] = 350.0
    assert server_mod._density_delta(500.0) == pytest.approx(0.01)  # medium


def test_remember_body_action_feeds_the_density_sink():
    before = len(server_mod._DENSITY_EVENTS)
    server_mod._remember_body_action("turn", {"status": "ok", "applied": True})
    server_mod._remember_body_action("walk", {"status": "ok", "applied": True})
    server_mod._remember_body_action("walk", {"status": "error", "applied": False})
    assert len(server_mod._DENSITY_EVENTS) == before + 2
    stamps = [ts for ts, _kind in server_mod._DENSITY_EVENTS[-2:]]
    assert all(abs(ts - time.time()) < 5.0 for ts in stamps)
    server_mod._DENSITY_STATE["last_round_at"] = stamps[0] - 1.0
    server_mod._DENSITY_STATE["last_user_turn_at"] = 0.0
    assert server_mod._density_level(time.time()) == "medium"


# ── pet_set_tempo tool ──────────────────────────────────────────────


class _FunctionCallProvider:
    """Function-calling provider (same shape as OpenAI/DeepSeek)."""

    name = "deepseek"
    supports_function_calling = True
    allow_tools = True

    def __init__(self, first_turn_events: list[dict]) -> None:
        self._first_turn_events = first_turn_events
        self.calls: list[dict[str, Any]] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            for event in self._first_turn_events:
                yield event
        else:
            yield {"type": "delta", "content": "好的。"}
        yield {"type": "end"}


def _run_function_chat(provider, mcp, monkeypatch, tools) -> list[dict]:
    monkeypatch.setattr(server_mod, "_pet_action", _no_action)
    monkeypatch.setattr(server_mod, "_resolve_provider", lambda *a, **k: provider)
    monkeypatch.setattr(server_mod, "_kernel", lambda: None, raising=False)
    monkeypatch.setattr(server_mod, "get_event_store", lambda: None, raising=False)
    monkeypatch.setattr(server_mod, "_extraction_hook", None)

    async def _tools(_mcp_manager, _req, core_only=False):
        return tools

    monkeypatch.setattr(server_mod, "_gather_tools", _tools)
    req = _req(tools_enabled=True)

    async def _drain():
        frames: list[dict] = []
        async for chunk in server_mod._stream_chat_provider(
            _StubRegistry(provider), mcp, _StubBridge(), req,
            server=None, server_state={}, mood_store=_StubMood(),
            lora_arr=None,
        ):
            text = chunk.decode("utf-8")
            if text.startswith("data: "):
                frames.append(json.loads(text[6:]))
        return frames

    return asyncio.run(_drain())


async def _no_action(*_a, **_k):
    return {"status": "not_applied", "applied": False}


def test_pet_set_tempo_writes_store_and_emits_tempo_event(monkeypatch):
    provider = _FunctionCallProvider([{
        "type": "tool_call",
        "id": "tc_tempo",
        "server_name": "pet-tempo",
        "name": "pet_set_tempo",
        "arguments": {"mode": "fast"},
    }])
    frames = _run_function_chat(
        provider, _StubMCP(), monkeypatch,
        [ToolDef(server_name="pet-tempo", name="pet_set_tempo")],
    )

    tempo = _tempo_frames(frames)
    assert tempo and tempo[-1]["mode"] == "fast"
    assert server_mod._TEMPO_STATE["mode"] == "fast"
    assert server_mod._TEMPO_STATE["source"] == "model"
    assert server_mod._TEMPO_STATE["expires_at"] > time.time() + 500
    # Chinese summary reaches the model's follow-up feedback.
    feedback = json.dumps(provider.calls[-1]["messages"], ensure_ascii=False)
    assert "反应节奏：快反应（约10分钟后回落）" in feedback
    assert frames[-1]["event"] == "end"


def test_pet_set_tempo_invalid_mode_falls_back_to_slow(monkeypatch):
    provider = _FunctionCallProvider([{
        "type": "tool_call",
        "id": "tc_tempo_bad",
        "server_name": "pet-tempo",
        "name": "pet_set_tempo",
        "arguments": {"mode": "daze"},
    }])
    frames = _run_function_chat(
        provider, _StubMCP(), monkeypatch,
        [ToolDef(server_name="pet-tempo", name="pet_set_tempo")],
    )

    tempo = _tempo_frames(frames)
    assert tempo and tempo[-1]["mode"] == "slow"
    assert server_mod._TEMPO_STATE["mode"] == "slow"
    feedback = json.dumps(provider.calls[-1]["messages"], ensure_ascii=False)
    assert "反应节奏：慢反应（约10分钟后回落）" in feedback


def test_pet_set_tempo_tool_schema_has_three_modes():
    import asyncio as _asyncio

    class _FakeKernel:
        def all_tools(self):
            return {}

    class _FakeMcp:
        async def list_all_tools(self):
            return []

    async def _collect():
        return await server_mod._gather_tools(
            _FakeMcp(), _req(tools_enabled=True), core_only=True
        )

    tools = _asyncio.run(_collect()) or []
    tempo_tool = next(t for t in tools if t.name == "pet_set_tempo")
    assert tempo_tool.input_schema["properties"]["mode"]["enum"] == [
        "ultra", "fast", "slow"
    ]
    assert "slow=慢反应" in tempo_tool.description
    assert "fast=快反应" in tempo_tool.description
    assert "ultra=极快反应" in tempo_tool.description
    assert "默认慢反应" in tempo_tool.description


# ── Chinese tool status labels ──────────────────────────────────────


def test_tool_label_map_and_fallback():
    assert server_mod._tool_label("memory") == "记忆"
    assert server_mod._tool_label("recall") == "回忆"
    assert server_mod._tool_label("capture_screen") == "看屏幕"
    assert server_mod._tool_label("pet_set_tempo") == "调节奏"
    assert server_mod._tool_label("totally_unknown") == "totally_unknown"


def test_memory_tool_call_shows_chinese_label_and_keeps_raw_meta(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)
    brain_mod.clear()

    async def _passive(*_a, **_kw):
        return _decision()

    provider = _RecordingOpenAI([
        [{
            "type": "tool_call",
            "id": "tc_mem",
            "server_name": "builtin",
            "name": "memory",
            "arguments": {"action": "list"},
        }, {"type": "end"}],
        [{"type": "delta", "content": "看完了。"}, {"type": "end"}],
    ])
    _install(monkeypatch, provider, passive=_passive)
    mcp = _ResultMCP({"is_error": False, "summary": "ok"})

    req = _req(tools_enabled=True)

    async def _tools(_mcp_manager, _req, core_only=False):
        return [ToolDef(server_name="builtin", name="memory")]

    monkeypatch.setattr(server_mod, "_gather_tools", _tools)
    frames = _run_with_mcp(provider, req, mcp)

    deltas = "".join(f.get("content", "") for f in frames if f.get("event") == "delta")
    assert "_执行 记忆..._" in deltas
    assert "✓ 记忆 完成" in deltas

    events = [e for e in brain_mod.snapshot() if e.get("type") == "tool_call"]
    assert events and events[0]["title"] == "工具 · 记忆"
    assert events[0]["meta"]["tool"] == "memory"  # raw id stays machine-readable
    results = [e for e in brain_mod.snapshot() if e.get("type") == "tool_result"]
    assert results and results[0]["title"] == "结果 · 记忆"
    assert results[0]["meta"]["tool"] == "memory"


def test_narration_filter_strips_chinese_label_status_lines(monkeypatch):
    from gateway.think_filter import ToolNarrationFilter

    f = ToolNarrationFilter()
    text = "在呢。\n_执行 记忆..._\n_执行 调节奏..._\n好啦。\n"
    out = "".join(f.feed(text)) + f.flush()
    assert out == "在呢。\n好啦。\n"
    # Raw-id asides keep working too.
    f2 = ToolNarrationFilter()
    out2 = "".join(f2.feed("x\n_执行 recall..._\ny\n")) + f2.flush()
    assert out2 == "x\ny\n"
    # Normal prose with 执行 is never swallowed.
    f3 = ToolNarrationFilter()
    out3 = "".join(f3.feed("执行任务很重要\n")) + f3.flush()
    assert out3 == "执行任务很重要\n"
