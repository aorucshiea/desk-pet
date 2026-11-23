"""Integration wiring: vitals (电量/血量/睡眠) + ledger (500 轮台账) in the
FastAPI chat pipeline.

Covers the 11-point wiring contract:
- every provider-reached round drains battery exactly once (prompt + completion
  estimate) and ticks the ledger exactly once;
- proactive rounds are short-circuited while forced_off / asleep / died (no
  provider call, no tick, no drain), while real user turns always go through;
- user speech during sleep wakes via ``user_speech_wake``: somatic text into
  the system prompt, stage 1 forces thinking off even when D says otherwise;
- the 500-mark organize round replaces the life prompt (audit text, core
  memory tools only / recap, no tools), records ``organize`` and stores the
  recap for the next real user turn;
- low-battery rest reminder (and slow-overflow escalation) injected into the
  system prompt;
- /api/health exposes the ``vitals`` / ``ledger`` objects.

The harness follows test_gateway_cortex.py / test_gateway_tempo.py: a scripted
OpenAIProvider subclass drives the real ``_stream_chat_provider``, the app is
built via ``build_app`` (conftest pins PET_MEMORY_DIR at tmp), and the
singletons are reinstalled over the built app's own via ``set_vitals`` /
``set_ledger`` so every assertion reads the exact objects under test.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from gateway import brain as brain_mod  # noqa: F401 (subscribe side effects)
from gateway import server as server_mod
from gateway import telemetry as telemetry_mod
from gateway.decision import cortex
from gateway.decision.cortex import DStateStore
from gateway.decision.reflex import ReflexConfig
from gateway.ledger import Ledger
from gateway.providers.openai import OpenAIProvider
from gateway.server import ChatRequest
from gateway.vitals import Vitals


# ── Stubs (same shape as test_gateway_cortex.py) ────────────────────


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
    # Never let a leftover request-scoped reflex config (or a built app's
    # boot singleton) arm the reflex gate in these tests.
    server_mod._reflex_config_ref.pop("cfg", None)
    server_mod.clear_reflex_config_override()

    async def _no_body(*_a, **_k):
        return {"status": "not_applied", "applied": False}

    async def _no_world(*_a, **_k):
        return []

    monkeypatch.setattr(server_mod, "_pet_action", _no_body)
    monkeypatch.setattr(server_mod, "_world_bodies_for_decision", _no_world)
    monkeypatch.setattr(server_mod, "_format_vision_context", lambda: "")
    yield
    # The override lives in a main-thread ContextVar: without this teardown
    # it would leak into every later test file in the same pytest process.
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


def _passive(**overrides):
    """Async passive_decision stub (the real one is awaited)."""

    async def _decide(*_a, **_k):
        return _decision(**overrides)

    return _decide


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


# ── Singleton installation helper ───────────────────────────────────


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


# ── 1. A proactive round drains battery + records a slow entry ──────


def test_proactive_round_drains_battery_and_ticks_ledger(life, monkeypatch):
    vitals, ledger = life
    percent_before = vitals.percent()
    provider = _RecordingOpenAI([[
        {"type": "delta", "content": "今天也过得不错。"},
        {"type": "end"},
    ]])
    _install(monkeypatch, provider, passive=lambda *a, **k: _decision())
    frames = _run(provider, _req(messages=_PROACTIVE))

    assert frames[-1]["event"] == "end"
    # Battery dropped by the round's estimated cost.
    assert vitals.percent() < percent_before
    # The drain matches prompt + completion estimate for a single round:
    # units = tokens × temperature × 1.0 (no thinking).
    assert len(provider.calls) == 1
    temperature = float(provider.calls[0]["temperature"])
    # Exact arithmetic, recomputed from the same sources the finally uses.
    observed_prompt = 338  # the prompt-budget log line above
    expected_completion = server_mod._estimate_tokens("今天也过得不错。")
    expected_units = (observed_prompt + expected_completion) * temperature
    expected_percent = max(0.0, percent_before - expected_units / vitals.capacity * 100.0)
    assert vitals.percent() == pytest.approx(expected_percent, abs=1e-6)
    # Ledger: exactly one slow entry, round_count advanced by 1.
    assert ledger.round_count() == 1
    counts = ledger.counts()
    assert counts["slow"] == 1
    assert counts["fast"] == 0, "fast entries belong to the future reflex layer"
    assert counts["organize"] == 0
    entry = ledger.entries()[-1]
    assert "今天也过得不错" in entry["text"]


def test_user_round_drains_once_and_records_slow(life, monkeypatch):
    vitals, ledger = life
    provider = _RecordingOpenAI([[
        {"type": "delta", "content": "我在呢。"},
        {"type": "end"},
    ]])
    _install(monkeypatch, provider, passive=lambda *a, **k: _decision())
    frames = _run(provider, _req())

    assert frames[-1]["event"] == "end"
    assert vitals.percent() < 100.0
    assert ledger.round_count() == 1
    assert ledger.counts()["slow"] == 1


# ── 2. Forced-off short-circuit ─────────────────────────────────────


def test_forced_off_short_circuits_proactive_round(life, monkeypatch):
    vitals, ledger = life
    vitals.drain_main(tokens=10**9, thinking=False, temperature=0.6)
    assert vitals.forced_off

    provider = _RecordingOpenAI([[{"type": "delta", "content": "不该出现"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=lambda *a, **k: _decision())
    frames = _run(provider, _req(messages=_PROACTIVE))

    # Renderer contract: machine sleep_state event (no visible bubble), end,
    # no provider call. No work = no round: no ledger tick, no further drain.
    assert _events(frames) == ["start", "sleep_state", "end"]
    assert frames[1] == {"event": "sleep_state", "state": "forced_off"}
    assert _visible(frames) == ""
    assert provider.calls == []
    assert ledger.round_count() == 0
    assert ledger.entries() == []

    # A real user turn is NEVER blocked by the forced-off latch.
    frames_user = _run(provider, _req())
    assert frames_user[-1]["event"] == "end"
    assert len(provider.calls) == 1
    assert ledger.round_count() == 1
    # The user round drained whatever was left (still latched at 0 units).
    assert vitals.percent() == 0.0


# ── 3. Asleep suppression + user-speech wake ────────────────────────


def test_asleep_proactive_short_circuits_and_user_turn_wakes(life, monkeypatch):
    vitals, ledger = life
    vitals.start_sleep(3600.0)
    assert vitals.is_asleep()

    provider = _RecordingOpenAI([[{"type": "delta", "content": "不该出现"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=lambda *a, **k: _decision())
    frames = _run(provider, _req(messages=_PROACTIVE))

    assert _events(frames) == ["start", "sleep_state", "end"]
    assert frames[1] == {"event": "sleep_state", "state": "asleep"}
    assert _visible(frames) == ""
    assert provider.calls == []
    assert ledger.round_count() == 0

    # User turn wakes the pet: somatic text in the round's system prompt,
    # stage 1 forces thinking OFF even though D asked for it, and the
    # sleep episode continues (extended inside user_speech_wake).
    hp_before = vitals.hp
    remaining_before = vitals.sleep_remaining_s()
    stage1_somatic = "需要睡觉"
    provider_user = _RecordingOpenAI([[
        {"type": "delta", "content": "嗯……谁在叫我。"},
        {"type": "end"},
    ]])
    _install(
        monkeypatch, provider_user,
        passive=lambda *a, **k: _decision(thinking=True, reasoning_effort="low"),
    )
    frames_user = _run(provider_user, _req())

    assert frames_user[-1]["event"] == "end"
    assert len(provider_user.calls) == 1
    system = provider_user.calls[0]["system"]
    assert stage1_somatic in system
    assert "【身体此刻的感受】" in system
    # Stage 1 → think False → thinking_override False reaches the provider.
    assert provider_user.calls[0].get("thinking_override") is False
    # The wake cost 1.0 HP and extended the plan.
    assert vitals.hp == pytest.approx(hp_before - 1.0)
    assert vitals.sleep_remaining_s() > remaining_before
    assert vitals.is_asleep()


def test_stage2_wake_keeps_thinking_on(life, monkeypatch):
    vitals, _ledger = life
    vitals.start_sleep(3600.0)
    vitals.user_speech_wake()  # stage 1
    provider = _RecordingOpenAI([[
        {"type": "delta", "content": "醒了，头好痛。"},
        {"type": "end"},
    ]])
    _install(
        monkeypatch, provider,
        passive=lambda *a, **k: _decision(thinking=False),
    )
    frames = _run(provider, _req())
    assert frames[-1]["event"] == "end"
    assert provider.calls[0].get("thinking_override") is True
    assert "头疼" in (provider.calls[0]["system"] or "")


# ── 4. Organize round (audit) + recap attach ────────────────────────


def test_organize_audit_round_prompt_tools_and_record(life, monkeypatch):
    vitals, ledger = life
    # Pre-fill the ledger to the 500 mark: 495 slow entries already there,
    # and round_count brought to 499 — the round's own tick is #500 and
    # fires organize_due + "audit" (record_slow does NOT tick).
    for i in range(495):
        ledger.record_slow(f"旧记忆 {i}")
    for _ in range(499):
        ledger.round_tick()

    # The 500th round: D speaks, provider replies with a visible audit note.
    provider = _RecordingOpenAI([[
        {"type": "delta", "content": "检查过了，记忆完好。"},
        {"type": "end"},
    ]])
    _install(monkeypatch, provider, passive=lambda *a, **k: _decision())
    frames = _run(provider, _req(messages=_PROACTIVE))
    assert frames[-1]["event"] == "end"

    # The tick from round 500 fired the mark; the pending slot now holds it.
    assert server_mod._pending_vitals_round["organize_kind"] == "audit"

    # The NEXT proactive round is the organize round: audit text in the
    # system prompt, tools narrowed to the core memory trio.
    provider2 = _RecordingOpenAI([[
        {"type": "delta", "content": "整理完成：核心记忆无缺漏。"},
        {"type": "end"},
    ]])

    gathered: list[Any] = []

    async def _gather_tools(mcp_manager, req, core_only=False):
        from gateway.providers.base import ToolDef
        tools = [
            ToolDef(server_name="builtin", name="memory", description="记忆", input_schema={"type": "object", "properties": {}}),
            ToolDef(server_name="builtin", name="recall", description="回忆", input_schema={"type": "object", "properties": {}}),
            ToolDef(server_name="builtin", name="memory_search", description="搜记忆", input_schema={"type": "object", "properties": {}}),
            ToolDef(server_name="builtin", name="capture_screen", description="看屏幕", input_schema={"type": "object", "properties": {}}),
            ToolDef(server_name="pet-world", name="pet_world_bodies", description="刚体", input_schema={"type": "object", "properties": {}}),
            ToolDef(server_name="pet", name="pet_decision", description="直觉", input_schema={"type": "object", "properties": {}}),
        ]
        gathered.append([t.name for t in tools])
        if core_only:
            return [t for t in tools if t.name in server_mod.CORE_TOOL_NAMES]
        return tools

    monkeypatch.setattr(server_mod, "_gather_tools", _gather_tools)
    monkeypatch.setattr(server_mod, "_resolve_provider", lambda *_a, **_k: provider2)

    frames2 = _run(provider2, _req(messages=_PROACTIVE, tools_enabled=True))
    assert frames2[-1]["event"] == "end"
    system2 = provider2.calls[0]["system"]
    assert "记忆整理时刻" in system2
    assert "不要调用屏幕或桌面工具" in system2
    # Tools were narrowed for THIS round's provider call.
    sent_tools = provider2.calls[0].get("tools") or []
    sent_names = {t.name for t in sent_tools}
    assert sent_names == {"memory", "recall", "memory_search"}
    # record_organize happened and the pending slot was cleared.
    assert ledger.counts()["organize"] == 1
    assert "整理完成" in ledger.entries()[-1]["text"]
    assert server_mod._pending_vitals_round["organize_kind"] is None


def test_organize_recap_round_sets_recap_and_user_turn_consumes_it(life, monkeypatch):
    vitals, ledger = life
    # 995 slow entries + 999 ticks → the round's own tick is #1000 and
    # fires organize_due + "recap".
    for i in range(995):
        ledger.record_slow(f"记录 {i}")
    for _ in range(999):
        ledger.round_tick()

    provider = _RecordingOpenAI([[
        {"type": "delta", "content": "这轮回顾我整理了文件夹，想到还没回复用户的问题。"},
        {"type": "end"},
    ]])
    _install(monkeypatch, provider, passive=_passive())
    frames = _run(provider, _req(messages=_PROACTIVE))
    assert frames[-1]["event"] == "end"
    assert server_mod._pending_vitals_round["organize_kind"] == "recap"

    # Seed >100 MIXED entries so the recap material must show exactly the
    # LAST 100 (mixed fast/slow/organize lines, newest last).
    for i in range(60):
        ledger.record_fast(f"快直觉记录 {i}")
    for i in range(60):
        ledger.record_slow(f"主脑记录 {i}")
    ledger.record_organize("上一次的整理产物")

    # Next proactive round = recap organize round: summary prompt with the
    # last-100 ledger lines, NO tools.
    provider2 = _RecordingOpenAI([[
        {"type": "delta", "content": "回顾：整理了记忆，想到有个问题没回。"},
        {"type": "end"},
    ]])
    monkeypatch.setattr(server_mod, "_resolve_provider", lambda *_a, **_k: provider2)
    frames2 = _run(provider2, _req(messages=_PROACTIVE, tools_enabled=True))
    assert frames2[-1]["event"] == "end"
    system2 = provider2.calls[0]["system"]
    assert "回顾时刻" in system2
    assert "最近 100 轮生命台账" in system2
    # Exactly the last 100 entries: the oldest of them is present, the
    # ones pushed out are gone. (996 slow after round 1 + 60 fast + 60
    # slow + 1 organize = 1117 total → last 100 start at the 22nd fast
    # entry, i.e. "快直觉记录 21".)
    assert "快直觉记录 21" in system2
    assert "快直觉记录 20" not in system2
    assert "主脑记录 59" in system2
    assert "上一次的整理产物" in system2
    assert "[快]" in system2 and "[主脑]" in system2 and "[整理]" in system2
    assert provider2.calls[0].get("tools_enabled") is False
    # recap stored via set_recap (consume_recap returns the text once).
    recap = ledger.consume_recap()
    assert recap is not None
    assert "回顾" in recap
    assert ledger.consume_recap() is None

    # Refill the recap for the attach test: the next REAL user turn gets
    # 【你上次的回顾】 + the stored text, exactly once.
    ledger.set_recap("之前你问我天气，我还没回。")
    provider3 = _RecordingOpenAI([[
        {"type": "delta", "content": "你好！我在。"},
        {"type": "end"},
    ]])
    monkeypatch.setattr(server_mod, "_resolve_provider", lambda *_a, **_k: provider3)
    frames3 = _run(provider3, _req())
    assert frames3[-1]["event"] == "end"
    assert "【你上次的回顾】" in (provider3.calls[0]["system"] or "")
    assert "之前你问我天气" in (provider3.calls[0]["system"] or "")
    assert ledger.consume_recap() is None
    # A following user turn no longer carries the recap (one-shot).
    frames4 = _run(provider3, _req())
    assert frames4[-1]["event"] == "end"
    assert "【你上次的回顾】" not in (provider3.calls[1]["system"] or "")


# ── 5. Rest reminder injection ──────────────────────────────────────


def test_rest_warning_injected_into_proactive_round(life, monkeypatch):
    vitals, _ledger = life
    # Drain to below the 25% warn threshold.
    vitals.drain_main(tokens=int(vitals.capacity * 0.8), thinking=False, temperature=1.0)
    assert vitals.should_warn()
    percent = vitals.percent()

    provider = _RecordingOpenAI([[
        {"type": "delta", "content": "有点累了。"},
        {"type": "end"},
    ]])
    _install(monkeypatch, provider, passive=lambda *a, **k: _decision())
    frames = _run(provider, _req(messages=_PROACTIVE))
    assert frames[-1]["event"] == "end"

    system = provider.calls[0]["system"]
    assert "系统提示：你的电量剩余" in system
    assert f"{percent:.1f}" in system
    assert "连续工作太久会强制停机" in system


def test_overflow_escalation_beats_low_battery_warning(life, monkeypatch):
    vitals, ledger = life
    # 400+ slow entries → the NEXT round_tick latches the overflow and
    # publishes it into the pending slot.
    for i in range(400):
        ledger.record_slow(f"慢记录 {i}")
    provider0 = _RecordingOpenAI([[{"type": "delta", "content": "好"}, {"type": "end"}]])
    _install(monkeypatch, provider0, passive=lambda *a, **k: _decision())
    frames0 = _run(provider0, _req(messages=_PROACTIVE))
    assert frames0[-1]["event"] == "end"
    assert server_mod._pending_vitals_round["overflow"] is True

    # Even with a full battery the escalation (not the battery warn) fires.
    assert not vitals.should_warn()
    provider = _RecordingOpenAI([[
        {"type": "delta", "content": "我需要休息。"},
        {"type": "end"},
    ]])
    monkeypatch.setattr(server_mod, "_resolve_provider", lambda *_a, **_k: provider)
    frames = _run(provider, _req(messages=_PROACTIVE))
    assert frames[-1]["event"] == "end"
    system = provider.calls[0]["system"]
    assert "过度劳累开始清理旧的记忆" in system
    assert "系统提示：你的电量剩余" not in system


# ── 6. Quiet rounds: ledger stub, no organize mark leak ─────────────


def test_quiet_round_records_stub_and_still_ticks(life, monkeypatch, tmp_path):
    vitals, ledger = life
    # D-quiet needs an ACTIVE D store (a proactive quiet round with no D
    # store just falls through to a normal speak round).
    store = DStateStore(tmp_path / "dstate.json")
    store.summary = "上一轮总结"
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)
    provider = _RecordingOpenAI([[{"type": "delta", "content": "[INNER:内心的想法]"}, {"type": "end"}]])
    _install(
        monkeypatch, provider,
        passive=lambda *a, **k: _decision(action="quiet"),
    )
    frames = _run(provider, _req(messages=_PROACTIVE))
    # D-quiet: no provider call, no tick, no drain.
    assert _events(frames)[-1] == "end"
    assert provider.calls == []
    assert ledger.round_count() == 0
    assert vitals.percent() == 100.0


def test_proactive_silent_round_ledger_stub(life, monkeypatch):
    vitals, ledger = life
    # A proactive speak round whose model output is inner-voice only
    # (quiet life turn): the ledger records the inner voice.
    provider = _RecordingOpenAI([[
        {"type": "delta", "content": "[INNER:今天安静地想了一些事]"},
        {"type": "end"},
    ]])
    _install(monkeypatch, provider, passive=lambda *a, **k: _decision())
    frames = _run(provider, _req(messages=_PROACTIVE))
    assert frames[-1]["event"] == "end"
    assert ledger.round_count() == 1
    text = ledger.entries()[-1]["text"]
    assert text.strip() != ""
    assert "安静的一轮" in text or "今天安静地想了一些事" in text


# ── 7. Death: user wake path archives + proactive stays blocked ─────


def test_death_via_wake_archives_and_resets(life, monkeypatch, tmp_path):
    vitals, ledger = life
    # A couple of entries from the life that is about to end.
    ledger.record_slow("旧灵魂的记录一")
    ledger.record_slow("旧灵魂的记录二")
    vitals.start_sleep(3600.0)
    # Burn the HP down to exactly one wake away from death.
    vitals.damage(99.0, "测试耗损")
    assert vitals.hp == pytest.approx(1.0)
    assert not vitals.died

    provider = _RecordingOpenAI([[
        {"type": "delta", "content": "谁在叫我……"},
        {"type": "end"},
    ]])
    _install(monkeypatch, provider, passive=lambda *a, **k: _decision())

    events_path = Path(server_mod._vitals_ref["dir"]) / "events.json"
    events_path.write_text(json.dumps({"events": [{"title": "生前的事"}]}, ensure_ascii=False), encoding="utf-8")

    frames = _run(provider, _req())
    assert frames[-1]["event"] == "end"
    # The wake killed the pet; the gateway archived into _grave-*.
    graves = list(Path(server_mod._vitals_ref["dir"]).glob("_grave-*"))
    assert len(graves) == 1
    assert (graves[0] / "_death-note.json").is_file()
    assert (graves[0] / "events.json").is_file()
    # Fresh body: hp restored, died cleared, and the archive runs only once.
    assert not vitals.died
    assert vitals.hp == 100.0
    assert server_mod._should_archive_death(vitals) is False
    # The round still completed as a normal user turn.
    assert len(provider.calls) == 1
    # The in-memory ledger was REBUILT after the archive (the test's local
    # `ledger` is the dead soul's stale object): the fresh singleton loads
    # the reset file — the old soul's entries are gone, only this round's
    # own slow record (from the finally) exists — and it keeps writing.
    fresh_ledger = server_mod.get_ledger()
    assert fresh_ledger is not ledger
    texts = [e["text"] for e in fresh_ledger.entries()]
    assert all("旧灵魂的记录" not in t for t in texts), texts
    fresh_ledger.record_slow("新身体的第一次记录")
    data = json.loads((Path(server_mod._vitals_ref["dir"]) / "ledger.json").read_text(encoding="utf-8"))
    assert data["entries"][-1]["text"] == "新身体的第一次记录"
    assert data["seq"] >= 2  # seq continues (reset file preserved it)


def test_dead_body_blocks_proactive_rounds(life, monkeypatch):
    vitals, ledger = life
    vitals.damage(200.0, "测试死亡")
    assert vitals.died
    provider = _RecordingOpenAI([[{"type": "delta", "content": "不该出现"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=lambda *a, **k: _decision())
    frames = _run(provider, _req(messages=_PROACTIVE))
    assert _events(frames) == ["start", "sleep_state", "end"]
    assert frames[1] == {"event": "sleep_state", "state": "died"}
    assert _visible(frames) == ""
    assert provider.calls == []
    assert ledger.round_count() == 0


# ── 8. /api/health exposes vitals + ledger ──────────────────────────


def test_health_endpoint_reports_vitals_and_ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("PET_THEME", "default")
    monkeypatch.setattr(server_mod.ClawdBridge, "post", lambda *_a, **_k: None)

    async def _no_mcp_config(self):
        return {}

    monkeypatch.setattr(server_mod.MCPManager, "load_config", _no_mcp_config)
    app = server_mod.build_app(initial_model=None)

    built_vitals = server_mod.get_vitals()
    built_ledger = server_mod.get_ledger()
    assert built_vitals is not None and built_ledger is not None
    built_ledger.record_slow("健康检查")

    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        data = client.get("/api/health").json()
    assert data["vitals"] is not None
    v = data["vitals"]
    for key in ("battery_percent", "forced_off", "should_warn", "hp", "died", "asleep", "sleep_stage"):
        assert key in v
    assert v["battery_percent"] == pytest.approx(built_vitals.percent(), abs=0.2)
    assert v["asleep"] is False
    assert data["ledger"] is not None
    l = data["ledger"]
    assert l["round_count"] == built_ledger.round_count()
    assert l["counts"]["slow"] == 1
    assert l["pending_recap"] is False
    # vitals.json / ledger.json live beside the memory base (events.json dir).
    base = Path(os.environ["PET_MEMORY_DIR"])
    assert (base / "vitals.json").is_file()
    assert (base / "ledger.json").is_file()

    # Teardown: clear the module singletons this app installed.
    server_mod.set_vitals(None)
    server_mod.set_ledger(None)
    server_mod._vitals_ref.pop("dir", None)


# ── 8b. Theme switch: vitals/ledger follow the body (soul isolation) ──


def test_theme_switch_rebuilds_vitals_and_ledger(tmp_path, monkeypatch):
    """Switching themes rebinds the body singletons: the new soul starts
    from its OWN vitals.json/ledger.json, the old theme's files are never
    written again, and the grave-dir pointer follows."""
    monkeypatch.setenv("PET_THEME", "default")
    monkeypatch.setattr(server_mod.ClawdBridge, "post", lambda *_a, **_k: None)

    async def _no_mcp_config(self):
        return {}

    monkeypatch.setattr(server_mod.MCPManager, "load_config", _no_mcp_config)
    app = server_mod.build_app(initial_model=None)
    try:
        from fastapi.testclient import TestClient
        with TestClient(app) as client:
            base = Path(os.environ["PET_MEMORY_DIR"])
            boot_vitals = server_mod.get_vitals()
            boot_ledger = server_mod.get_ledger()
            # Body A (default theme) burns some battery and ticks rounds.
            boot_vitals.drain_main(tokens=2000, thinking=False, temperature=1.0)
            boot_ledger.record_slow("主题A的身体记录")
            battery_a = boot_vitals.percent()
            vitals_a_path = base / "vitals.json"
            ledger_a_path = base / "ledger.json"
            vitals_a_mtime = vitals_a_path.stat().st_mtime_ns
            ledger_a_before = json.loads(ledger_a_path.read_text(encoding="utf-8"))

            data = client.post("/api/memory/switch", json={"theme": "theme-b"}).json()
            assert data["ok"] is True
            assert data["theme"] == "theme-b"

            new_vitals = server_mod.get_vitals()
            new_ledger = server_mod.get_ledger()
            # Rebuilt singletons on theme B's dir.
            assert new_vitals is not boot_vitals
            assert new_ledger is not boot_ledger
            b_dir = base / "themes" / "theme-b"
            assert Path(server_mod._vitals_state_dir()) == b_dir
            # Fresh soul: B's battery is full (no inheritance from A).
            assert new_vitals.percent() == pytest.approx(100.0)
            assert new_ledger.round_count() == 0
            # A subsequent drain writes into B's dir.
            new_vitals.drain_main(tokens=100, thinking=False, temperature=1.0)
            assert (b_dir / "vitals.json").is_file()
            assert json.loads((b_dir / "vitals.json").read_text(encoding="utf-8"))["units"] < json.loads((b_dir / "vitals.json").read_text(encoding="utf-8"))["capacity"]

            # A's files were NOT further modified by B's writes.
            assert vitals_a_path.stat().st_mtime_ns == vitals_a_mtime
            assert json.loads(ledger_a_path.read_text(encoding="utf-8")) == ledger_a_before
            # /api/health now reports B's fresh battery.
            health = client.get("/api/health").json()
            assert health["vitals"]["battery_percent"] == pytest.approx(
                new_vitals.percent(), abs=0.2
            )
    finally:
        server_mod.set_vitals(None)
        server_mod.set_ledger(None)
        server_mod._vitals_ref.pop("dir", None)
        server_mod._reflex_config_ref.pop("cfg", None)
        server_mod.clear_reflex_config_override()


# ── 9. Charge tick branch (unit-level: the loop body is inline) ─────


def test_charge_branch_semantics(life, monkeypatch, tmp_path):
    """The health-loop branch charges asleep fast / awake slow and wakes."""
    vitals, _ledger = life
    vitals.drain_main(tokens=int(vitals.capacity * 0.9), thinking=False, temperature=1.0)
    p_awake_start = vitals.percent()
    # Awake idle minute.
    vitals.charge(1.0, asleep=False)
    gain_awake = vitals.percent() - p_awake_start
    assert gain_awake == pytest.approx(0.05, abs=0.01)
    # Asleep minute.
    vitals.start_sleep(600.0)
    p_sleep_start = vitals.percent()
    vitals.charge(1.0, asleep=True)
    gain_sleep = vitals.percent() - p_sleep_start
    assert gain_sleep == pytest.approx(0.5, abs=0.01)
    assert vitals.is_asleep()
    # natural_wake only when the timer ran out.
    assert vitals.natural_wake() is False
    # Force-check the wake semantics on a separate instance. start_sleep
    # clamps a self-decided plan to ≥4h, so wake needs a ≥4h future "now".
    asleep_vitals = Vitals(tmp_path / "nw", context_limit=1000)
    asleep_vitals.start_sleep(1.0)
    assert asleep_vitals.natural_wake() is False
    assert asleep_vitals.natural_wake(time.time() + 5 * 3600.0) is True
    assert not asleep_vitals.is_asleep()


# ── 10. Organize-integrity: the 500-round cycle must never be lost ──


def test_pending_organize_survives_reflex_quiet_round(life, monkeypatch, tmp_path):
    """A pending organize mark turns the next proactive round into the
    organize round EVEN when the reflex gate would call it quiet."""
    vitals, ledger = life
    store = DStateStore(tmp_path / "dstate.json")
    store.summary = "整理前夜"
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)
    # 499 ticks done → the next round's tick is #500; the PREVIOUS round's
    # tick already published the mark into the pending slot (that is the
    # wiring contract the round start reads).
    for _ in range(499):
        ledger.round_tick()
    server_mod._pending_vitals_round["organize_kind"] = "audit"
    judge_calls: list[dict] = []

    async def _judge(state, config, **kwargs):
        judge_calls.append({"state": dict(state), "config": config})
        return {"action": "quiet", "think": False, "next_check_s": 300,
                "reason": "", "source": "reflex"}

    monkeypatch.setattr(server_mod._reflex_module, "judge", _judge)
    server_mod.set_reflex_config_override(
        ReflexConfig(enabled=True, max_tokens=128)
    )
    provider = _RecordingOpenAI([[
        {"type": "delta", "content": "检查过了，记忆完好。"},
        {"type": "end"},
    ]])
    gathered: list[Any] = []

    async def _gather_tools(mcp_manager, req, core_only=False):
        from gateway.providers.base import ToolDef
        tools = [
            ToolDef(server_name="builtin", name="memory", description="记忆", input_schema={"type": "object", "properties": {}}),
            ToolDef(server_name="builtin", name="capture_screen", description="看屏幕", input_schema={"type": "object", "properties": {}}),
        ]
        gathered.append([t.name for t in tools])
        if core_only:
            return [t for t in tools if t.name in server_mod.CORE_TOOL_NAMES]
        return tools

    _install(monkeypatch, provider, passive=_passive(action="quiet"))
    monkeypatch.setattr(server_mod, "_gather_tools", _gather_tools)

    frames = _run(provider, _req(messages=_PROACTIVE, tools_enabled=True))

    # The organize round ran: provider called, audit prompt in the system,
    # core-only tools, organize entry recorded — the reflex gate never
    # consulted its judge.
    assert frames[-1]["event"] == "end"
    assert len(provider.calls) == 1
    assert judge_calls == []
    assert "记忆整理时刻" in provider.calls[0]["system"]
    assert gathered and gathered[0] == ["memory", "capture_screen"]
    sent = {t.name for t in (provider.calls[0].get("tools") or [])}
    assert sent == {"memory"}
    assert ledger.counts()["organize"] == 1


def test_user_turn_with_pending_organize_is_not_hijacked(life, monkeypatch, tmp_path):
    """A real user turn during a pending organize: answered normally, with
    full tools; the slot stays pending for the next proactive round."""
    vitals, ledger = life
    store = DStateStore(tmp_path / "dstate.json")
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)
    server_mod._pending_vitals_round["organize_kind"] = "audit"
    server_mod.set_reflex_config_override(ReflexConfig(enabled=True))
    provider = _RecordingOpenAI([[
        {"type": "delta", "content": "我在呢，怎么了？"},
        {"type": "end"},
    ]])
    gathered: list[Any] = []

    async def _gather_tools(mcp_manager, req, core_only=False):
        from gateway.providers.base import ToolDef
        tools = [
            ToolDef(server_name="builtin", name="memory", description="记忆", input_schema={"type": "object", "properties": {}}),
            ToolDef(server_name="builtin", name="capture_screen", description="看屏幕", input_schema={"type": "object", "properties": {}}),
        ]
        gathered.append([t.name for t in tools])
        return tools

    _install(monkeypatch, provider, passive=_passive())
    monkeypatch.setattr(server_mod, "_gather_tools", _gather_tools)

    frames = _run(provider, _req(tools_enabled=True))

    # Answered as asked: no organize prompt, FULL tools, normal slow entry.
    assert frames[-1]["event"] == "end"
    assert len(provider.calls) == 1
    system = provider.calls[0]["system"]
    assert "记忆整理时刻" not in system
    assert gathered and gathered[0] == ["memory", "capture_screen"]
    sent = {t.name for t in (provider.calls[0].get("tools") or [])}
    assert sent == {"memory", "capture_screen"}
    assert ledger.counts()["organize"] == 0
    # The slot was NOT consumed by the user turn (no tick overwrite either).
    assert server_mod._pending_vitals_round["organize_kind"] == "audit"

    # The NEXT proactive round consumes it and runs the organize round.
    provider2 = _RecordingOpenAI([[
        {"type": "delta", "content": "检查过了，记忆完好。"},
        {"type": "end"},
    ]])
    monkeypatch.setattr(server_mod, "_resolve_provider", lambda *_a, **_k: provider2)
    frames2 = _run(provider2, _req(messages=_PROACTIVE, tools_enabled=True))
    assert frames2[-1]["event"] == "end"
    assert len(provider2.calls) == 1
    assert "记忆整理时刻" in provider2.calls[0]["system"]
    assert ledger.counts()["organize"] == 1
    assert server_mod._pending_vitals_round["organize_kind"] is None
