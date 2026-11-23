"""Gateway wiring for the D core (cortex.py) and the hidden DSTATE block.

The gateway asks D once per turn (before provider resolution). Proactive
quiet ends without resolving or calling an LLM; think keeps actual inner
voice off the speech surface. LLM rounds apply D's thinking/reaction budget,
inject the previous summary plus decision prompt, and parse the main LLM's
end-of-round DSTATE block back into the per-theme store. These tests drive the real
``_stream_chat_provider`` with a scripted provider and a test D store.

No D store (direct unit tests, legacy callers) must keep the old
behaviour: no injection, no D call, no budget override.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from gateway import brain as brain_mod
from gateway import server as server_mod
from gateway import telemetry
from gateway.decision import cortex
from gateway.decision.cortex import DStateStore
from gateway.providers.openai import OpenAIProvider
from gateway.server import ChatRequest


# ── Stubs ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_life_state(monkeypatch):
    monkeypatch.setattr(server_mod, "_dstate_provider", None)
    monkeypatch.setattr(server_mod, "_body_log_ref", None)
    monkeypatch.setattr(server_mod, "_ephemeral_ref", None)
    monkeypatch.setattr(server_mod, "_last_user_msg", "")
    monkeypatch.setattr(server_mod, "_last_user_ts", 0.0)
    monkeypatch.setattr(server_mod, "_last_real_user_ts", 0.0)

    async def _no_body(*_a, **_k):
        return {"status": "not_applied", "applied": False}

    async def _no_world(*_a, **_k):
        return []

    # Control tags are body abilities even with tools_enabled=False; tests
    # must not send them to a running user's desktop.
    monkeypatch.setattr(server_mod, "_pet_action", _no_body)
    monkeypatch.setattr(server_mod, "_world_bodies_for_decision", _no_world)
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


def _dstate(summary: str) -> str:
    return (
        "<<<DSTATE>>>"
        + json.dumps({"summary": summary}, ensure_ascii=False)
        + "<<<DSTATEEND>>>"
    )


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


def _use_store(monkeypatch, tmp_path) -> DStateStore:
    store = DStateStore(Path(tmp_path) / "dstate.json")
    store.summary = "上一轮总结：用户在调试网关"
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)
    return store


def _visible(frames: list[dict]) -> str:
    return "".join(
        frame.get("content", "")
        for frame in frames
        if frame.get("event") == "delta"
    )


# ── Visible-text scrubbing: a MEM tag is never speech ──────────────


def test_visible_reply_text_strips_stray_mem_tags():
    assert server_mod._visible_reply_text("<<<MEMEND>>>") == ""
    assert server_mod._visible_reply_text("正文<<<MEMEND>>>") == "正文"
    assert server_mod._visible_reply_text("正文<<<MEM>>>") == "正文"
    assert server_mod._visible_reply_text("<<<memend>>>") == ""
    # DSTATE uses the same never-visible rule: a stray closer left by a
    # block split across tool rounds must not reach the bubble either.
    assert server_mod._visible_reply_text("<<<DSTATEEND>>>") == ""
    assert server_mod._visible_reply_text("正文<<<DSTATEEND>>>") == "正文"
    assert server_mod._visible_reply_text("正文<<<DSTATE>>>") == "正文"
    # Complete blocks are still stripped, not just their markers.
    assert (
        server_mod._visible_reply_text(
            '前文<<<MEM>>>{"events": []}<<<MEMEND>>>后文'
        )
        == "前文后文"
    )


def test_visible_reply_text_strips_hallucinated_tool_markup():
    assert server_mod._visible_reply_text('正文<tool_call>{name:1}</tool_call>后续') == '正文后续'
    assert server_mod._visible_reply_text('正文<tool_call>\n<tool_call>\n') == '正文'
    assert server_mod._visible_reply_text('正文</tool_call>') == '正文'
    assert server_mod._visible_reply_text('正文<TOOL_CALL>幻觉</TOOL_CALL>后续') == '正文后续'


@pytest.mark.parametrize("terminated", [False, True])
def test_hallucinated_tool_call_markup_never_reaches_surface_or_record(monkeypatch, tmp_path, terminated):
    store = _use_store(monkeypatch, tmp_path)
    actions = []

    async def _act(kind, **payload):
        actions.append((kind, payload))
        return {"status": "ok", "applied": True}

    async def _passive(*_a, **_kw):
        return _decision()

    monkeypatch.setattr(server_mod, "_pet_action", _act)
    if terminated:
        text = '我在呢。<tool_call>{"name":"x","body":"[WALK:80,0]"}</tool_call>一直都在。'
    else:
        text = '我在呢。' + '<tool_call>\n' * 6
    provider = _RecordingOpenAI([[
        {"type": "delta", "content": text[:12]},
        {"type": "delta", "content": text[12:]},
        {"type": "end"},
    ]])
    _install(monkeypatch, provider, passive=_passive)
    frames = _run(provider, _req())
    expected = "我在呢。一直都在。" if terminated else "我在呢。"
    assert _visible(frames) == expected
    assert store.last_round["spoken"] == expected
    assert "tool_call" not in store.summary
    assert expected in store.summary
    assert actions == []
    assert store.last_round["receipts"] == []

# ── D applied to the provider call + prompt injection ──────────────


def test_set_dstate_provider_installs_and_resets(tmp_path):
    store = DStateStore(Path(tmp_path) / "dstate.json")
    try:
        server_mod.set_dstate_provider(lambda: store)
        assert server_mod._get_dstate_store() is store
        server_mod.set_dstate_provider(None)
        assert server_mod._get_dstate_store() is None
    finally:
        server_mod.set_dstate_provider(None)


def test_provider_failure_returns_none():
    def _boom():
        raise RuntimeError("no theme yet")

    try:
        server_mod.set_dstate_provider(_boom)
        assert server_mod._get_dstate_store() is None
    finally:
        server_mod.set_dstate_provider(None)


def test_cortex_can_be_disabled_by_env(monkeypatch, tmp_path):
    assert server_mod._cortex_enabled() is True
    monkeypatch.setenv("PET_CORTEX_ENABLED", "0")
    assert server_mod._cortex_enabled() is False

    _use_store(monkeypatch, tmp_path)
    calls: list[Any] = []

    async def _passive(*_a, **_kw):
        calls.append(1)
        return _decision()

    provider = _RecordingOpenAI([[{"type": "delta", "content": "好"}], [{"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)

    _run(provider, _req())

    assert calls == []
    assert "【你的底层直觉状态" not in (provider.calls[0]["system"] or "")


def test_d_decision_applies_budget_and_injects_previous_summary(monkeypatch, tmp_path):
    store = _use_store(monkeypatch, tmp_path)
    states: list[dict] = []

    async def _passive(_store, state=None, **kwargs):
        states.append(dict(state or {}))
        assert kwargs["timeout"] == server_mod.CORTEX_DECISION_TIMEOUT_USER
        return _decision()

    provider = _RecordingOpenAI([[{"type": "delta", "content": "好"}], [{"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)

    req = _req(thinking=False)
    frames = _run(provider, req)

    assert any(frame.get("event") == "end" for frame in frames)
    assert states and states[0]["user_text"] == "今天怎么样"
    assert states[0]["is_proactive"] is False

    call = provider.calls[0]
    assert call["thinking_override"] is True
    assert call["reasoning_effort_override"] == "low"
    assert call["max_tokens"] == 2048
    assert req.thinking is True
    assert req.reasoning_effort == "low"
    assert req.max_new_tokens == 2048

    system = call["system"]
    assert "【你的底层直觉状态" in system
    assert "上一轮总结：用户在调试网关" in system
    assert "【这一轮你的底层直觉】" in system
    assert "默认动作：说" in system
    assert "max_tokens=2048" in system


@pytest.mark.parametrize("action", ["quiet", "think"])
def test_quiet_action_is_forced_to_speak_on_a_real_user_turn(monkeypatch, tmp_path, action):
    _use_store(monkeypatch, tmp_path)
    decision = _decision(action=action)

    async def _passive(_store, state=None, **_kw):
        return dict(decision)

    provider = _RecordingOpenAI([[{"type": "delta", "content": "好"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)

    frames = _run(provider, _req())

    assert len(provider.calls) == 1
    system = provider.calls[0]["system"]
    assert "默认动作：说" in system
    assert f"默认动作：{decision['action_label']}" not in system
    assert "这一轮是心声模式" not in system
    assert _visible(frames) == "好"
    assert frames[-1]["event"] == "end"


def test_proactive_turn_keeps_quiet_default(monkeypatch, tmp_path):
    store = _use_store(monkeypatch, tmp_path)
    store.record_round("previous", 990.0, 995.0, True, "", "上一轮真实心声", [])
    previous_round = dict(store.last_round)
    previous_summary = store.summary
    monkeypatch.setattr(server_mod.time, "time", lambda: 1000.0)
    timeouts: list[float] = []
    states: list[dict] = []

    async def _passive(_store, state=None, **kwargs):
        assert _store is store
        states.append(dict(state or {}))
        timeouts.append(kwargs["timeout"])
        return _decision(action="quiet")

    provider = _RecordingOpenAI([[{"type": "delta", "content": "绝不能生成正文"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)

    def _forbidden_provider(*_a, **_kw):
        pytest.fail("proactive quiet must end before provider resolution")

    monkeypatch.setattr(server_mod, "_resolve_provider", _forbidden_provider)
    brain_mod.clear()
    frames = _run(provider, _req(messages=[{"role": "user", "content": "[系统提示：该说话了]"}]))

    assert provider.calls == []
    assert len(states) == 1 and states[0]["is_proactive"] is True
    assert states[0]["last_round"] == previous_round
    decisions = [e for e in brain_mod.snapshot() if e.get("type") == "d_decision"]
    assert len(decisions) == 1
    assert decisions[0]["meta"]["action"] == "quiet"
    assert decisions[0]["meta"]["decision_id"] == "d_test"
    assert frames[0]["event"] == "start"
    assert frames[-1]["event"] == "end"
    assert any(f.get("event") == "tempo" for f in frames)
    assert _visible(frames) == ""
    assert all(not f.get("text") and not f.get("content") for f in frames if f.get("event") == "inner")
    assert store.last_round["round_id"] != previous_round["round_id"]
    assert store.last_round["proactive"] is True
    assert store.last_round["spoken"] == store.last_round["inner"] == ""
    assert store.last_round["started_at"] == store.last_round["finished_at"] == 1000.0
    assert store.summary == previous_summary
    assert store.questions == []
    assert DStateStore(store.path).last_round == store.last_round
    assert timeouts == [server_mod.CORTEX_DECISION_TIMEOUT]
    assert server_mod.CORTEX_DECISION_TIMEOUT == 6.0
    assert server_mod.CORTEX_DECISION_TIMEOUT_USER == 2.5


def test_tag_only_proactive_round_is_quiet_not_speak(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        return _decision(action="speak", action_label="说")

    # The model split its MEM block across tool rounds and this final round
    # contains only the closing marker.
    provider = _RecordingOpenAI(
        [[{"type": "delta", "content": "<<<MEMEND>>>"}, {"type": "end"}]]
    )
    _install(monkeypatch, provider, passive=_passive)
    brain_mod.clear()

    frames = _run(
        provider,
        _req(messages=[{"role": "user", "content": "[系统提示：该说话了]"}]),
    )

    # The quiet/speak classification is a brain-stream event, not a chat
    # delta: the surface must stay silent either way.
    brain_events = brain_mod.snapshot()
    assert [e for e in brain_events if e.get("type") == "speak"] == []
    quiet = [e for e in brain_events if e.get("type") == "quiet"]
    assert quiet and quiet[-1]["meta"].get("reason") == "proactive_no_text"
    assert server_mod._visible_reply_text(_visible(frames)) == ""


# ── DSTATE end-of-round contract ────────────────────────────────────


def test_dstate_block_is_hidden_and_updates_the_store(monkeypatch, tmp_path):
    store = _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        return _decision()

    block = _dstate("新的总结：用户在写网关测试")
    provider = _RecordingOpenAI(
        [
            [
                {"type": "delta", "content": "我在呢。"},
                {"type": "delta", "content": "<<<DSTATE>>>{\"summary\":\"新的"},
                {"type": "delta", "content": "总结：用户在写网关测试\"}<<<DSTATEEND>>>"},
                {"type": "end"},
            ]
        ]
    )
    _install(monkeypatch, provider, passive=_passive)

    frames = _run(provider, _req())

    visible = _visible(frames)
    assert "我在呢。" in visible
    assert "DSTATE" not in visible
    assert "新的总结" not in visible
    assert store.summary == "新的总结：用户在写网关测试"
    assert store.path.exists()
    assert "新的总结" in store.path.read_text(encoding="utf-8")
    assert block  # sanity: the fixture string is what the provider streamed


def test_next_round_injects_the_new_summary(monkeypatch, tmp_path):
    store = _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        return _decision()

    provider = _RecordingOpenAI(
        [
            [
                {"type": "delta", "content": _dstate("第二轮的新总结")},
                {"type": "end"},
            ]
        ]
    )
    _install(monkeypatch, provider, passive=_passive)

    _run(provider, _req())
    assert store.summary == "第二轮的新总结"

    # Round 2: the newly persisted summary is what the main LLM sees.
    _run(provider, _req())
    assert "第二轮的新总结" in provider.calls[1]["system"]
    assert "上一轮总结：用户在调试网关" not in provider.calls[1]["system"]


def test_unterminated_dstate_is_stripped_even_without_a_store(monkeypatch):
    provider = _RecordingOpenAI(
        [
            [
                {"type": "delta", "content": "答案"},
                {"type": "delta", "content": '<<<DSTATE>>>{"summary":"没写完'},
                {"type": "end"},
            ]
        ]
    )
    _install(monkeypatch, provider)
    monkeypatch.setattr(server_mod, "_dstate_provider", None)

    frames = _run(provider, _req())

    visible = _visible(frames)
    assert visible == "答案"
    assert "DSTATE" not in visible


# ── No D store = legacy behaviour ──────────────────────────────────


def test_no_store_means_no_injection_and_legacy_budget(monkeypatch):
    calls: list[Any] = []

    async def _passive(*_a, **_kw):
        calls.append(1)
        return _decision()

    provider = _RecordingOpenAI([[{"type": "delta", "content": "好"}], [{"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)
    monkeypatch.setattr(server_mod, "_dstate_provider", None)

    # Code fence + 报错: the legacy meta seat keeps thinking=True here.
    req = _req(
        messages=[
            {
                "role": "user",
                "content": "```python\nraise KeyError(1)\n```\n为什么报错？",
            }
        ],
        thinking=True,
        reasoning_effort="high",
    )
    _run(provider, req)

    assert calls == [], "D must not be consulted without a store"
    system = provider.calls[0]["system"] or ""
    assert "【你的底层直觉状态" not in system
    # Legacy cloud floor and the requested thinking effort survive.
    assert provider.calls[0]["max_tokens"] == 8192
    assert provider.calls[0]["thinking_override"] is True
    assert provider.calls[0]["reasoning_effort_override"] == "high"


def test_d_exception_falls_back_to_safe_budget(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        raise RuntimeError("laya down")

    provider = _RecordingOpenAI([[{"type": "delta", "content": "好"}], [{"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)

    _run(provider, _req())

    call = provider.calls[0]
    assert call["thinking_override"] is False
    assert call["reasoning_effort_override"] is None
    assert call["max_tokens"] == cortex.REACTION_MAX_TOKENS["medium"]
    assert "【这一轮你的底层直觉】" in call["system"]


# ── pet_enter_slow: same-round slow-intuition upgrade ──────────────


def test_pet_enter_slow_upgrades_the_rest_of_the_round(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        return _decision(
            thinking=False,
            reasoning_effort=None,
            strength="no_thinking",
        )

    provider = _RecordingOpenAI(
        [
            [
                {
                    "type": "tool_call",
                    "id": "tc_slow",
                    "server_name": "default",
                    "name": "pet_enter_slow",
                    "arguments": {"strength": "high"},
                },
                {"type": "end"},
            ],
            [{"type": "delta", "content": "想清楚了。\n"}, {"type": "end"}],
        ]
    )
    _install(monkeypatch, provider, passive=_passive)

    before = telemetry.snapshot()["counters"].get("decision.slow.enter", 0)
    frames = _run(provider, _req())
    after = telemetry.snapshot()["counters"].get("decision.slow.enter", 0)

    assert after == before + 1
    assert len(provider.calls) == 2
    # D started the round without thinking; the tool upgraded the REST of it.
    assert provider.calls[0]["thinking_override"] is False
    assert provider.calls[1]["thinking_override"] is True
    assert provider.calls[1]["reasoning_effort_override"] == "high"

    follow_up = json.dumps(provider.calls[1]["messages"], ensure_ascii=False, default=str)
    # The tool result is fed back as its full content (not only the English
    # summary) so data-bearing tools keep their payload.
    assert "high" in follow_up
    assert "慢直觉" in follow_up or "slow intuition" in follow_up
    assert any(frame.get("event") == "delta" for frame in frames)


def test_slow_and_active_decision_tools_are_core():
    assert "pet_enter_slow" in server_mod.CORE_TOOL_NAMES
    assert "pet_decision" in server_mod.CORE_TOOL_NAMES


# ── pet_decision: active D tool (never mutates the store) ───────────


def test_pet_decision_tool_returns_json_and_summary(monkeypatch, tmp_path):
    store = DStateStore(Path(tmp_path) / "dstate.json")
    store.last_decision = {"action": "look"}
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: store)

    seen: list[tuple] = []

    async def _active_active(_store, state=None, **kwargs):
        seen.append((state, kwargs))
        return _decision(strength="low", reaction="small", action="look")

    monkeypatch.setattr(cortex, "active_decision", _active_active)
    token = server_mod._active_user_text_ref.set("看这边")

    try:
        before = telemetry.snapshot()["counters"].get("decision.cortex.active", 0)
        result = asyncio.run(
            server_mod._decision_tool(
                {
                    "context": "桌面上开着编辑器",
                    "action_options": [
                        {"id": "observe", "label": "看"},
                        {"id": "chat", "label": "聊"},
                    ],
                }
            )
        )
        after = telemetry.snapshot()["counters"].get("decision.cortex.active", 0)
    finally:
        server_mod._active_user_text_ref.reset(token)

    assert result["is_error"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["decision_id"] == "d_test"
    assert result["summary"] == "主动 D → low/small/look"
    assert after == before + 1

    state, kwargs = seen[0]
    assert state["user_text"] == "看这边"
    assert state["is_proactive"] is False
    assert state["now"] > 0
    assert state["last_round"] == {}
    assert "last_action" not in state  # last_decision=look was only a proposal
    assert kwargs["context"] == "桌面上开着编辑器"
    assert kwargs["action_options"][0]["id"] == "observe"
    assert kwargs["timeout"] == server_mod.CORTEX_DECISION_TIMEOUT == 6.0
    # active_decision does not save: the on-disk store was never created.
    assert store.path.exists() is False


def test_pet_decision_tool_missing_store_is_soft(monkeypatch):
    monkeypatch.setattr(server_mod, "_dstate_provider", None)

    result = asyncio.run(server_mod._decision_tool({}))

    assert result["is_error"] is True
    assert "D 还不可用" in result["content"][0]["text"]


# ── Think proactive rounds vs. the visible fallback ────────────────


def _tool_then_catchup_provider(catchup_tail: str) -> _RecordingOpenAI:
    """Round 1 calls a tool; the forced tools=None catch-up says ``catchup_tail``."""
    return _RecordingOpenAI(
        [
            [
                {
                    "type": "tool_call",
                    "id": "tc_recall",
                    "server_name": "builtin",
                    "name": "recall",
                    "arguments": {"keyword": "用户"},
                },
                {"type": "end"},
            ],
            [{"type": "end"}],
            [{"type": "delta", "content": catchup_tail}, {"type": "end"}],
        ]
    )


def test_think_proactive_round_may_end_silently_and_keeps_dstate(monkeypatch, tmp_path):
    store = _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        return _decision(action="think", action_label="想")

    provider = _tool_then_catchup_provider(
        _dstate("安静地继续活着") + "[NEXT_CHAT:30]"
    )
    _install(monkeypatch, provider, passive=_passive)

    frames = _run(
        provider,
        _req(messages=[{"role": "user", "content": "[系统提示：该说话了]"}]),
    )

    assert len(provider.calls) == 3
    assert provider.calls[-1]["tools"] is None
    assert "这一轮是心声模式" in provider.calls[0]["system"]
    text = _visible(frames)
    assert server_mod.TOOL_LOOP_FALLBACK_TEXT not in text
    # The think life turn also hides the "_执行 recall..._" tool aside.
    assert "delta" not in [frame["event"] for frame in frames]
    next_chat = [f for f in frames if f.get("event") == "next_chat"]
    assert len(next_chat) == 1
    assert next_chat[0]["seconds"] == 30
    assert next_chat[0]["explicit"] is True
    assert store.summary == "安静地继续活着"
    assert store.path.exists()
    assert frames[-1]["event"] == "end"


def test_real_user_turn_still_gets_the_visible_fallback(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)

    async def _passive(_store, state=None, **_kw):
        return _decision()

    provider = _tool_then_catchup_provider("")
    _install(monkeypatch, provider, passive=_passive)

    frames = _run(provider, _req())

    text = _visible(frames)
    assert server_mod.TOOL_LOOP_FALLBACK_TEXT in text
    assert frames[-1]["event"] == "end"


# ── Quiet skips the LLM; think keeps actual inner voice ────────────


class _FakeEphemeral:
    """Records add_thought calls instead of writing a theme memory dir."""

    memory_dir = None

    def __init__(self) -> None:
        self.thoughts: list[tuple[str, str]] = []

    def format_for_prompt(self) -> str:
        return ""

    def add_thought(self, text, source="dice", confidence=0.0) -> None:
        self.thoughts.append((source, text))


_PROACTIVE = [{"role": "user", "content": "[系统提示：该说话了]"}]


def test_idle_quiet_preserves_summary_and_s_without_inner_generation(monkeypatch, tmp_path):
    store = _use_store(monkeypatch, tmp_path)
    store.questions = [
        {"q": "要继续休息吗？", "options": ["休息", "不休息"], "weight": "light"},
        {"q": "要观察桌面吗？", "options": ["观察", "不观察"], "weight": "heavy"},
    ]
    store.note_user("已经回答的问题", "earlier", 900.0)
    store.record_round("earlier", 900.0, 910.0, False, "之前的回答", "之前的真实心声", [])
    store.save()
    previous = store.to_dict()
    monkeypatch.setattr(server_mod.time, "time", lambda: 1000.0)
    ephemeral = _FakeEphemeral()
    monkeypatch.setattr(server_mod, "_ephemeral_ref", ephemeral)
    states: list[dict] = []
    timeouts: list[float] = []

    async def _passive(_store, state=None, **kwargs):
        assert _store is store
        states.append(dict(state or {}))
        timeouts.append(kwargs["timeout"])
        assert _store.questions == previous["questions"]
        return _decision(action="quiet", s_answers=[
            {"q": q["q"], "weight": q["weight"], "choice": q["options"][0], "confidence": 0.8}
            for q in _store.questions
        ])

    provider = _RecordingOpenAI([[
        {"type": "think", "content": "不应触发 API reasoning"},
        {"type": "delta", "content": "不应生成正文[INNER:不应生成心声]" + _dstate("不应覆盖总结")},
        {"type": "end"},
    ]])
    _install(monkeypatch, provider, passive=_passive)

    def _forbidden_provider(*_a, **_kw):
        pytest.fail("idle quiet must not resolve a provider")

    monkeypatch.setattr(server_mod, "_resolve_provider", _forbidden_provider)
    brain_mod.clear()
    frames = _run(provider, _req(messages=_PROACTIVE))

    assert provider.calls == []
    assert len(states) == 1 and states[0]["is_proactive"] is True
    assert states[0]["last_round"] == previous["last_round"]
    assert timeouts == [15.0]
    decisions = [e for e in brain_mod.snapshot() if e.get("type") == "d_decision"]
    assert len(decisions) == 1 and decisions[0]["meta"]["action"] == "quiet"
    assert frames[0]["event"] == "start" and frames[-1]["event"] == "end"
    assert len([f for f in frames if f.get("event") == "tempo"]) == 1
    signals = [f for f in frames if f.get("event") == "inner"]
    assert len(signals) == 1
    assert all(not f.get("text") and not f.get("content") for f in signals)
    assert not any(f.get("event") in {"delta", "think", "tool_call", "body_action"} for f in frames)
    assert _visible(frames) == "" and ephemeral.thoughts == []
    assert store.last_round["round_id"] != previous["last_round"]["round_id"]
    assert store.last_round["proactive"] is True
    assert store.last_round["spoken"] == store.last_round["inner"] == ""
    assert store.last_round["receipts"] == []
    assert store.last_round["started_at"] == store.last_round["finished_at"] == 1000.0
    assert store.last_user == previous["last_user"]
    assert store.summary == previous["summary"]
    assert store.questions == previous["questions"]
    disk = DStateStore(store.path)
    assert disk.last_round == store.last_round
    assert disk.last_user == previous["last_user"]
    assert disk.summary == previous["summary"]
    assert disk.questions == previous["questions"]


def test_decision_to_prompt_only_think_adds_the_inner_voice_instruction():
    for option in cortex.DEFAULT_ACTION_OPTIONS:
        prompt = cortex.decision_to_prompt(
            _decision(action=option["id"], action_label=option["label"])
        )
        assert "用户直接说话必须回应" in prompt
        assert f"默认动作：{option['label']}" in prompt
        if option["id"] == "think":
            assert "这一轮是心声模式" in prompt
            assert "[INNER:...]" in prompt
            assert "不要空着" in prompt
            assert "默认动作：想" in prompt
        else:
            assert "这一轮是心声模式" not in prompt
            assert "[INNER:...]" not in prompt
            assert "不要空着" not in prompt


def test_think_life_turn_hides_visible_text_but_stores_the_thought(
    monkeypatch, tmp_path
):
    store = _use_store(monkeypatch, tmp_path)
    ephemeral = _FakeEphemeral()
    monkeypatch.setattr(server_mod, "_ephemeral_ref", ephemeral)

    async def _passive(_store, state=None, **_kw):
        return _decision(action="think", action_label="想")

    provider = _RecordingOpenAI(
        [
            [
                {"type": "delta", "content": "窗外的光有点晃眼。"},
                {"type": "delta", "content": _dstate("安静地继续活着")},
                {"type": "end"},
            ]
        ]
    )
    _install(monkeypatch, provider, passive=_passive)

    before = telemetry.snapshot()["counters"].get("decision.quiet.life", 0)
    frames = _run(provider, _req(messages=_PROACTIVE))
    after = telemetry.snapshot()["counters"].get("decision.quiet.life", 0)

    assert len(provider.calls) == 1
    assert "这一轮是心声模式" in provider.calls[0]["system"]
    assert "默认动作：想" in provider.calls[0]["system"]
    assert "delta" not in [frame["event"] for frame in frames]
    assert frames[0]["event"] == "start"
    assert frames[-1]["event"] == "end"
    assert _visible(frames) == ""
    assert ("quiet_life", "（心声）窗外的光有点晃眼。") in ephemeral.thoughts
    assert store.last_round["spoken"] == ""
    assert store.last_round["inner"] == "窗外的光有点晃眼。"
    assert store.summary == "安静地继续活着"
    assert store.path.exists()
    assert after == before + 1


def test_think_life_turn_keeps_the_inner_event_without_double_storing(
    monkeypatch, tmp_path
):
    store = _use_store(monkeypatch, tmp_path)
    ephemeral = _FakeEphemeral()
    monkeypatch.setattr(server_mod, "_ephemeral_ref", ephemeral)

    async def _passive(_store, state=None, **_kw):
        return _decision(action="think", action_label="想")

    provider = _RecordingOpenAI(
        [
            [
                {"type": "delta", "content": "[INNER:桌面很安静，我有点困]"},
                {"type": "delta", "content": _dstate("困倦但平静")},
                {"type": "end"},
            ]
        ]
    )
    _install(monkeypatch, provider, passive=_passive)

    frames = _run(provider, _req(messages=_PROACTIVE))

    assert len(provider.calls) == 1
    assert "默认动作：想" in provider.calls[0]["system"]
    assert "delta" not in [frame["event"] for frame in frames]
    assert "inner" in [frame["event"] for frame in frames]
    assert ephemeral.thoughts == [
        ("inner", "（心声）桌面很安静，我有点困")
    ]
    assert store.last_round["spoken"] == ""
    assert store.last_round["inner"] == "桌面很安静，我有点困"
    assert store.summary == "困倦但平静"
    assert frames[-1]["event"] == "end"


@pytest.mark.parametrize("action", ["quiet", "think"])
def test_user_turn_quiet_decision_is_not_suppressed(monkeypatch, tmp_path, action):
    store = _use_store(monkeypatch, tmp_path)
    ephemeral = _FakeEphemeral()
    monkeypatch.setattr(server_mod, "_ephemeral_ref", ephemeral)

    async def _passive(_store, state=None, **_kw):
        return _decision(action=action)

    provider = _RecordingOpenAI(
        [
            [
                {"type": "delta", "content": "在的，我听见了。"},
                {"type": "delta", "content": _dstate("用户在和我说话")},
                {"type": "end"},
            ]
        ]
    )
    _install(monkeypatch, provider, passive=_passive)

    frames = _run(provider, _req())

    # Both quiet and think are forced to speak before life-mode suppression.
    assert len(provider.calls) == 1
    assert "默认动作：说" in provider.calls[0]["system"]
    assert "这一轮是心声模式" not in provider.calls[0]["system"]
    assert "在的，我听见了。" in _visible(frames)
    assert ephemeral.thoughts == []
    assert store.last_round["spoken"] == "在的，我听见了。"
    assert store.last_round["inner"] == ""
    assert store.last_user["responded_at"] is not None
    assert store.summary == "用户在和我说话"
    assert frames[-1]["event"] == "end"


def test_think_life_turn_suppresses_the_forced_catchup(monkeypatch, tmp_path):
    store = _use_store(monkeypatch, tmp_path)
    ephemeral = _FakeEphemeral()
    monkeypatch.setattr(server_mod, "_ephemeral_ref", ephemeral)

    async def _passive(_store, state=None, **_kw):
        return _decision(action="think", action_label="想")

    provider = _tool_then_catchup_provider("（有点困，先趴一会儿）[NEXT_CHAT:20]")
    _install(monkeypatch, provider, passive=_passive)

    frames = _run(provider, _req(messages=_PROACTIVE))

    assert "delta" not in [frame["event"] for frame in frames]
    assert _visible(frames) == ""
    assert len(provider.calls) == 3
    assert provider.calls[-1]["tools"] is None
    assert "默认动作：想" in provider.calls[0]["system"]
    assert ("quiet_life", "（心声）（有点困，先趴一会儿）") in ephemeral.thoughts
    assert store.last_round["spoken"] == ""
    assert store.last_round["inner"] == "（有点困，先趴一会儿）"
    assert frames[-1]["event"] == "end"


# ── Reflex S answers: the prompt only claims what the body did ─────


@pytest.mark.parametrize(
    "messages, receipt, expected_calls, note",
    [
        (_PROACTIVE, {"status": "ok", "applied": True}, 1, "身体已经转向左边"),
        (_PROACTIVE, {"status": "ok", "applied": False}, 1, "反射没执行"),
        (_PROACTIVE, {"status": "error", "applied": False}, 1, "反射没执行"),
        (
            [{"role": "user", "content": "今天怎么样"}],
            {"status": "ok", "applied": True},
            0,
            "反射没执行",
        ),
    ],
)
def test_reflex_turn_is_claimed_only_when_the_body_turned(
    monkeypatch, tmp_path, messages, receipt, expected_calls, note
):
    store = _use_store(monkeypatch, tmp_path)
    calls: list[tuple] = []

    async def _passive(_store, state=None, **_kw):
        return _decision(
            s_answers=[
                {"q": "往哪边看？", "weight": "reflex", "choice": "左", "confidence": 0.8}
            ]
        )

    async def _act(kind, **payload):
        calls.append((kind, payload))
        return dict(receipt, kind=kind, facing=payload.get("direction"))

    monkeypatch.setattr(server_mod, "_pet_action", _act)
    provider = _RecordingOpenAI([[{"type": "delta", "content": "好"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)

    _run(provider, _req(messages=messages))

    assert calls == [("turn", {"direction": "left"})] * expected_calls
    assert len(store.last_round["receipts"]) == expected_calls
    if expected_calls:
        saved = store.last_round["receipts"][0]
        assert saved["name"] == "reflex_turn"
        assert saved["applied"] is receipt["applied"]
        expected = "error" if receipt["status"] == "error" else ("ok" if receipt["applied"] else "not_applied")
        assert saved["status"] == expected
    system = provider.calls[0]["system"]
    assert cortex.S_ANSWERS_HEADER in system
    assert note in system
    if note == "反射没执行":
        assert "身体已经转向" not in system


def _life_req():
    return _req(messages=[
        {"role": "user", "content": "今天怎么样"},
        {"role": "assistant", "content": "我在呢。"},
        *_PROACTIVE,
    ])


@pytest.mark.parametrize("count, expected", [(0, 6.0), (2, 15.0), (6, 30.0)])
def test_automatic_d_budget_accounts_for_s_questions(monkeypatch, count, expected):
    monkeypatch.delenv("PET_CORTEX_DECISION_TIMEOUT", raising=False)
    monkeypatch.setattr(server_mod, "CORTEX_DECISION_TIMEOUT", 6.0)
    monkeypatch.setattr(server_mod, "CORTEX_DECISION_TIMEOUT_USER", 2.5)
    store = DStateStore()
    store.questions = [{"q": "要说吗？", "options": ["说", "不说"], "weight": "light"}] * count
    assert server_mod._cortex_round_timeout(store, True) == expected
    assert server_mod._cortex_round_timeout(store, False) == 2.5
    monkeypatch.setenv("PET_CORTEX_DECISION_TIMEOUT", "6")
    assert server_mod._cortex_round_timeout(store, True) == 6.0


def test_world_tool_acknowledgement_is_not_a_motor_receipt():
    receipt = server_mod._execution_receipt("pet_world_adopt", {"status": "ok"})
    assert receipt["status"] == "ok"
    assert receipt["kind"] == "tool"
    assert "applied" not in receipt
    assert "身体" not in receipt["summary"]


@pytest.mark.parametrize("result", [None, {}, {"status": "blocked"}, {"status": "cancelled"}])
def test_empty_or_blocked_tool_result_is_not_completed(result):
    assert server_mod._execution_receipt("capture_screen", result)["status"] != "ok"


def test_user_then_life_preserves_human_clock_and_answered_evidence(monkeypatch, tmp_path):
    store = _use_store(monkeypatch, tmp_path)
    now = [1000.0]
    monkeypatch.setattr(server_mod.time, "time", lambda: now[0])
    states = []

    async def _passive(_store, state, **_kw):
        states.append(dict(state))
        if not state["is_proactive"]:
            # Receipt is persisted BEFORE D runs, not just before provider.chat.
            disk = DStateStore(store.path)
            assert server_mod._last_user_msg == "今天怎么样"
            assert server_mod._last_real_user_ts == 1000.0
            assert server_mod._last_user_ts == 1000.0
            assert disk.last_user["received_at"] == 1000.0
            assert disk.last_user["responded_at"] is None
        return _decision(
            action="think" if state["is_proactive"] else "speak",
            action_label="想" if state["is_proactive"] else "说",
        )

    provider = _RecordingOpenAI([
        [{"type": "delta", "content": "我在呢。"}, {"type": "end"}],
        [{"type": "delta", "content": "[INNER:继续自己的念头]"}, {"type": "end"}],
    ])
    _install(monkeypatch, provider, passive=_passive)
    _run(provider, _req())
    first = dict(store.last_round)
    assert first["spoken"] == "我在呢。"
    assert store.last_user["round_id"] == first["round_id"]
    assert store.last_user["responded_at"] == 1000.0
    now[0] += 90
    frames = _run(provider, _life_req())
    assert len(provider.calls) == 2
    assert _visible(frames) == ""
    assert server_mod._last_user_msg == "今天怎么样"
    assert server_mod._last_real_user_ts == server_mod._last_user_ts == 1000.0
    assert states[1]["idle_sec"] == 90
    assert states[1]["now"] == 1090.0
    assert states[1]["last_round"] == first
    assert "last_action" not in states[1]
    assert store.last_user["responded_at"] == 1000.0
    assert store.last_round["spoken"] == ""
    assert store.last_round["inner"] == "继续自己的念头"
    for prompt in (provider.calls[1]["system"], provider.calls[1]["web_state"]):
        assert "90.0秒前收到；已回答" in prompt
        assert "我在呢。" in prompt
        assert "本轮没有新用户话" in prompt
    assert provider.calls[1]["system"].endswith("只有系统事实标为未回应的真人话仍待回应。")
    assert provider.calls[1]["messages"] == [
        {"role": "user", "content": "[系统提示：该说话了]"},
    ]
    assert "【此前已发生的对话（不是本轮新消息）】" in provider.calls[1]["system"]
    assert "用户：今天怎么样" in provider.calls[1]["system"]
    assert "你：我在呢。" in provider.calls[1]["system"]


@pytest.mark.parametrize("receipt, expected", [
    ({"status": "ok", "applied": True, "x": 12, "y": 34}, "ok"),
    ({"status": "ok", "applied": False}, "not_applied"),
    ({"status": "error", "applied": False}, "error"),
])
def test_control_body_receipts_come_from_executor_not_summary(monkeypatch, tmp_path, receipt, expected):
    from gateway.decision.body_log import BodyLog
    store = _use_store(monkeypatch, tmp_path)
    body_log = BodyLog()
    monkeypatch.setattr(server_mod, "_body_log_ref", body_log)
    actions = []

    async def _act(kind, **payload):
        actions.append((kind, payload))
        return dict(receipt)

    async def _passive(*_a, **_kw):
        return _decision()

    monkeypatch.setattr(server_mod, "_pet_action", _act)
    provider = _RecordingOpenAI([[
        {"type": "delta", "content": "[WALK:12,34][TURN:left]"},
        {"type": "delta", "content": _dstate("我已走到窗边并看见画面")},
        {"type": "end"},
    ]])
    _install(monkeypatch, provider, passive=_passive)
    frames = _run(provider, _req())  # tools_enabled=False still executes body tags
    assert actions == [("walk", {"dx": 12, "dy": 34}), ("turn", {"direction": "left"})]
    assert len([f for f in frames if f.get("event") == "body_action"]) == 2
    assert [r["status"] for r in store.last_round["receipts"]] == [expected, expected]
    assert all(r["applied"] is (expected == "ok") for r in store.last_round["receipts"])
    assert len(body_log.items) == (2 if expected == "ok" else 0)
    assert store.summary == "我已走到窗边并看见画面"  # subjective, not repaired by another LLM
    provider.script = [[{"type": "end"}]]
    _run(provider, _life_req())
    prompt = provider.calls[-1]["system"]
    if expected != "ok":
        assert "未完成（不能当作已执行）" in prompt
        assert "你走动了" not in prompt
    else:
        assert "身体已确认执行" in prompt


class _ResultMCP:
    def __init__(self, result, now=None):
        self.result = result
        self.now = now
        self.called = []

    async def call_tool(self, server_name, tool_name, arguments):
        self.called.append((server_name, tool_name, arguments))
        if self.now is not None:
            self.now[0] += 75
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _run_with_mcp(provider, req, mcp):
    async def _drain():
        frames = []
        async for chunk in server_mod._stream_chat_provider(
            _StubRegistry(provider), mcp, _StubBridge(), req,
            server=None, server_state={}, mood_store=_StubMood(),
        ):
            frames.append(json.loads(chunk.decode()[6:]))
        return frames
    return asyncio.run(_drain())


@pytest.mark.parametrize("tool_name, result, status", [
    ("capture_screen", {"is_error": True, "summary": "denied", "screenshot_base64": "PRIVATE"}, "error"),
    ("capture_screen", {"is_error": False, "summary": "No vision channel available"}, "not_applied"),
    ("capture_screen", {"is_error": False, "screenshot_base64": "PRIVATE", "summary": "image"}, "ok"),
    ("observe_screen", {"is_error": True, "summary": "PRIVATE SCREEN"}, "error"),
    ("observe_screen", {"is_error": False, "summary": "PRIVATE SCREEN", "parsed_content_list": ["PRIVATE"]}, "ok"),
    ("capture_screen", RuntimeError("PRIVATE PAYLOAD"), "error"),
])
def test_screen_receipts_and_live_tool_feedback(monkeypatch, tmp_path, tool_name, result, status):
    store = _use_store(monkeypatch, tmp_path)
    now = [1000.0]
    monkeypatch.setattr(server_mod.time, "time", lambda: now[0])
    store.note_user("今天怎么样", "earlier", 900.0)
    store.record_round("earlier", 900.0, 910.0, False, "我在呢。", "", [])

    async def _passive(*_a, **_kw):
        return _decision(action="think", action_label="想")

    provider = _RecordingOpenAI([
        [{"type": "tool_call", "id": "tc_screen", "server_name": "builtin", "name": tool_name,
          "arguments": {}}, {"type": "end"}],
        [{"type": "think", "content": "API reasoning should not be inner"},
         {"type": "delta", "content": "[INNER:想看但要以工具回执为准]"},
         {"type": "end"}],
    ])
    _install(monkeypatch, provider, passive=_passive)
    mcp = _ResultMCP(result, now)
    frames = _run_with_mcp(provider, _life_req(), mcp)
    assert mcp.called == [("builtin", tool_name, {})]
    assert len(provider.calls) == 2
    receipt = store.last_round["receipts"][0]
    assert receipt["status"] == status
    assert "PRIVATE" not in json.dumps(receipt)
    assert "base64" not in json.dumps(receipt)
    assert store.last_round["spoken"] == ""
    assert store.last_round["inner"] == "想看但要以工具回执为准"
    assert frames[-1]["event"] == "end"
    for key in ("system", "web_state"):
        assert "100.0秒前收到" in provider.calls[0][key]
        assert "175.0秒前收到" in provider.calls[1][key]
        assert f"{tool_name}: {status}" in provider.calls[1][key]
    feedback = json.dumps(provider.calls[1]["messages"], ensure_ascii=False)
    assert f"执行回执: {status}" in feedback
    if status != "ok":
        assert "没有获得可用图像/解析结果" in receipt["summary"]


def test_missing_dstate_still_saves_actual_last_round(monkeypatch, tmp_path):
    store = _use_store(monkeypatch, tmp_path)

    async def _passive(*_a, **_kw):
        return _decision()

    provider = _RecordingOpenAI([[
        {"type": "think", "content": "API reasoning"},
        {"type": "delta", "content": "<think>inline reasoning</think>我在呢。[INNER:心里记着]"},
        {"type": "end"},
    ]])
    _install(monkeypatch, provider, passive=_passive)
    _run(provider, _req())
    disk = DStateStore(store.path)
    assert disk.summary.startswith("【系统记录：本轮未提供模型主观总结】")
    assert "我实际说过" in disk.summary and "我在呢。" in disk.summary
    assert "心里记着" in disk.summary
    assert disk.last_round["spoken"] == "我在呢。"
    assert disk.last_round["inner"] == "心里记着"
    assert disk.last_user["responded_at"] is not None
    provider.script = [[{"type": "end"}]]
    _run(provider, _life_req())
    assert "当时实际说出：\"我在呢。\"" in provider.calls[1]["system"]
    assert "当时真实心声记录：\"心里记着\"" in provider.calls[1]["web_state"]


def test_think_round_keeps_hidden_text_across_tool_iterations(monkeypatch, tmp_path):
    store = _use_store(monkeypatch, tmp_path)

    async def _passive(*_a, **_kw):
        return _decision(action="think", action_label="想")

    provider = _RecordingOpenAI([
        [{"type": "delta", "content": "先留在心里。\n"},
         {"type": "tool_call", "id": "tc_think", "server_name": "builtin", "name": "recall", "arguments": {}},
         {"type": "end"}],
        [{"type": "think", "content": "API reasoning"},
         {"type": "delta", "content": "然后继续自己的念头。"}, {"type": "end"}],
    ])
    _install(monkeypatch, provider, passive=_passive)
    frames = _run_with_mcp(provider, _life_req(), _ResultMCP({"summary": "ok"}))
    assert _visible(frames) == ""
    assert len(provider.calls) == 2
    assert store.last_round["spoken"] == ""
    assert store.last_round["inner"] == "先留在心里。\n然后继续自己的念头。"
    assert store.last_round["receipts"][0]["name"] == "recall"


def test_subjective_action_without_execution_never_becomes_a_receipt(monkeypatch, tmp_path):
    store = _use_store(monkeypatch, tmp_path)
    store.last_decision = {"action": "walk"}

    async def _passive(*_a, **_kw):
        return _decision(action="think", action_label="想")

    provider = _RecordingOpenAI([[
        {"type": "delta", "content": "[INNER:想象我已走到窗边]" + _dstate("我已经走到窗边并看过截图")},
        {"type": "end"},
    ]])
    _install(monkeypatch, provider, passive=_passive)
    _run(provider, _req(messages=_PROACTIVE))
    assert store.last_round["receipts"] == []
    assert store.last_round["inner"] == "想象我已走到窗边"
    provider.script = [[{"type": "end"}]]
    _run(provider, _req(messages=_PROACTIVE))
    for key in ("system", "web_state"):
        assert "上一轮没有收到动作/感知完成回执" in provider.calls[1][key]
        assert "不是执行证据" in provider.calls[1][key]


@pytest.mark.parametrize("kind", ["DSTATE", "MEM"])
@pytest.mark.parametrize("lowercase", [False, True])
@pytest.mark.parametrize("terminated", [False, True])
def test_control_and_tool_markers_inside_hidden_blocks_do_not_execute(monkeypatch, tmp_path, kind, lowercase, terminated):
    store = _use_store(monkeypatch, tmp_path)
    actions = []

    async def _act(kind, **payload):
        actions.append((kind, payload))
        return {"status": "ok", "applied": True}

    async def _passive(*_a, **_kw):
        return _decision()

    monkeypatch.setattr(server_mod, "_pet_action", _act)
    subjective = "想象[WALK:80,0][TURN:left][INNER:只是幻想][MCP:builtin/capture_screen:{}]"
    opening, ending = f"<<<{kind}>>>", f"<<<{kind}END>>>"
    if lowercase:
        opening, ending = opening.lower(), ending.lower()
    payload = {"summary": subjective} if kind == "DSTATE" else {"events": [{"content": subjective}]}
    block = opening + json.dumps(payload, ensure_ascii=False) + (ending if terminated else "")
    provider = _RecordingOpenAI([[
        {"type": "delta", "content": "我在呢。" + block[:7]},
        {"type": "delta", "content": block[7:]},
        {"type": "end"},
    ]])
    _install(monkeypatch, provider, passive=_passive)
    mcp = _ResultMCP({"is_error": False, "summary": "should not be called"})
    frames = _run_with_mcp(provider, _req(tools_enabled=True), mcp)
    assert len(provider.calls) == 1
    assert _visible(frames) == "我在呢。"
    if kind == "DSTATE" and terminated:
        assert store.summary == subjective
    else:
        assert store.summary.startswith("【系统记录：本轮未提供模型主观总结】")
        assert "我在呢。" in store.summary
        assert "想象" not in store.summary
    assert actions == []
    assert mcp.called == []
    assert store.last_round["receipts"] == []
    assert store.last_round["inner"] == ""


def test_tool_outcome_is_visible_in_the_bubble(monkeypatch, tmp_path):
    _use_store(monkeypatch, tmp_path)

    async def _passive(*_a, **_kw):
        return _decision()

    call = {"type": "tool_call", "id": "tc_1", "server_name": "builtin",
            "name": "memory", "arguments": {"action": "list"}}

    provider = _RecordingOpenAI([[dict(call), {"type": "end"}], [{"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)
    frames = _run_with_mcp(
        provider, _req(tools_enabled=True), _ResultMCP({"is_error": False, "summary": "ok"})
    )
    deltas = "".join(f.get("content", "") for f in frames if f.get("event") == "delta")
    # Display label is Chinese (memory → 记忆); the raw id stays in meta.
    assert "✓ 记忆 完成" in deltas

    provider = _RecordingOpenAI([[dict(call, id="tc_2"), {"type": "end"}], [{"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)
    frames = _run_with_mcp(
        provider, _req(tools_enabled=True), _ResultMCP({"is_error": True, "summary": "boom"})
    )
    deltas = "".join(f.get("content", "") for f in frames if f.get("event") == "delta")
    assert "✗ 记忆 失败：boom" in deltas


@pytest.mark.parametrize("state_block", [True, False])
def test_failed_body_control_gets_same_round_feedback_without_replay(monkeypatch, tmp_path, state_block):
    store = _use_store(monkeypatch, tmp_path)
    attempts = []

    async def _act(kind, **payload):
        attempts.append((kind, payload))
        return {"status": "error", "applied": False}

    async def _passive(*_a, **_kw):
        return _decision()

    monkeypatch.setattr(server_mod, "_pet_action", _act)
    tail = _dstate("移动没有完成") if state_block else ""
    provider = _RecordingOpenAI([
        [{"type": "delta", "content": "[WALK:10,0]我挪过去啦。"}, {"type": "end"}],
        [{"type": "delta", "content": "[WALK:10,0]移动没有成功。" + tail}, {"type": "end"}],
    ])
    _install(monkeypatch, provider, passive=_passive)
    frames = _run(provider, _req(messages=[{"role": "user", "content": "向右走10像素"}]))
    assert attempts == [("walk", {"dx": 10, "dy": 0})]
    assert len(provider.calls) == 2
    assert provider.calls[1]["tools"] is None
    feedback = provider.calls[1]["messages"][-1]["content"]
    assert "身体执行回执" in feedback and '"applied": false' in feedback
    assert "不是新的用户消息" in feedback
    assert _visible(frames) == "移动没有成功。"
    assert store.last_round["spoken"] == "移动没有成功。"
    assert store.last_round["receipts"][0]["applied"] is False
    assert "挪过去啦" not in store.summary
    assert frames[-1]["event"] == "end"


def test_failed_forced_catchup_does_not_claim_a_completed_round(monkeypatch, tmp_path):
    store = _use_store(monkeypatch, tmp_path)

    async def _passive(*_a, **_kw):
        return _decision()

    provider = _RecordingOpenAI([
        [{"type": "tool_call", "id": "tc_catchup", "server_name": "builtin", "name": "recall", "arguments": {}},
         {"type": "end"}],
        [{"type": "end"}],
        [{"type": "error", "message": "catchup offline"}],
    ])
    _install(monkeypatch, provider, passive=_passive)
    brain_mod.clear()
    frames = _run(provider, _req())
    assert len(provider.calls) == 3
    assert provider.calls[-1]["tools"] is None
    assert frames[-1]["event"] == "error"
    assert store.last_round == {}
    assert store.last_user["responded_at"] is None
    assert not any(e["type"] in ("receipt", "round_end") for e in brain_mod.snapshot())


def test_life_evidence_is_theme_scoped(monkeypatch, tmp_path):
    a = DStateStore(tmp_path / "a.json")
    b = DStateStore(tmp_path / "b.json")
    active = [a]
    monkeypatch.setattr(server_mod, "_dstate_provider", lambda: active[0])

    async def _passive(*_a, **_kw):
        return _decision()

    provider = _RecordingOpenAI([[{"type": "delta", "content": "A的答复"}, {"type": "end"}]])
    _install(monkeypatch, provider, passive=_passive)
    _run(provider, _req(messages=[{"role": "user", "content": "A的私有问题"}]))
    active[0] = b
    provider.script = [[{"type": "end"}]]
    _run(provider, _req(messages=_PROACTIVE))
    for key in ("system", "web_state"):
        assert "A的私有问题" not in provider.calls[1][key]
        assert "A的答复" not in provider.calls[1][key]
    assert b.last_user == {}
    assert DStateStore(a.path).last_user["text"] == "A的私有问题"


def test_late_old_round_does_not_answer_interrupting_new_user(monkeypatch, tmp_path):
    store = _use_store(monkeypatch, tmp_path)

    async def _passive(*_a, **_kw):
        return _decision()

    class _InterleavedProvider(_RecordingOpenAI):
        async def chat(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                # A new real generator starts while this old generator awaits.
                async for _chunk in server_mod._stream_chat_provider(
                    _StubRegistry(self), _StubMCP(), _StubBridge(),
                    _req(messages=[{"role": "user", "content": "新问题"}]),
                    server=None, server_state={}, mood_store=_StubMood(),
                ):
                    pass
                yield {"type": "delta", "content": "迟到的旧问题答复"}
                yield {"type": "end"}
            else:
                yield {"type": "error", "message": "new turn interrupted"}

    provider = _InterleavedProvider([])
    _install(monkeypatch, provider, passive=_passive)
    _run(provider, _req(messages=[{"role": "user", "content": "旧问题"}]))
    disk = DStateStore(store.path)
    assert server_mod._last_user_msg == "新问题"
    assert disk.last_user["text"] == "新问题"
    assert disk.last_user["responded_at"] is None
    assert disk.last_round["spoken"] == "迟到的旧问题答复"
    assert disk.last_round["round_id"] != disk.last_user["round_id"]


def test_provider_error_does_not_record_completed_round(monkeypatch, tmp_path):
    store = _use_store(monkeypatch, tmp_path)

    async def _passive(*_a, **_kw):
        return _decision()

    provider = _RecordingOpenAI([[{"type": "error", "message": "offline"}]])
    _install(monkeypatch, provider, passive=_passive)
    brain_mod.clear()
    frames = _run(provider, _req())
    assert frames[-1]["event"] == "error"
    assert store.last_round == {}
    assert store.last_user["responded_at"] is None
    assert not any(e["type"] in ("receipt", "round_end") for e in brain_mod.snapshot())


def test_silent_request_leaves_life_state_and_human_clock_untouched(monkeypatch, tmp_path):
    store = _use_store(monkeypatch, tmp_path)
    store.note_user("真实用户", "r_real", 1000)
    store.record_round("r_real", 1000, 1001, False, "真实回答", "", [])
    before = store.to_dict()
    monkeypatch.setattr(server_mod, "_last_user_msg", "真实用户")
    monkeypatch.setattr(server_mod, "_last_real_user_ts", 1000)
    monkeypatch.setattr(server_mod, "_last_user_ts", 1000)
    provider = _RecordingOpenAI([[{"type": "delta", "content": _dstate("不应覆盖")}, {"type": "end"}]])
    _install(monkeypatch, provider)
    _run(provider, _req(silent=True))
    assert store.to_dict() == before
    assert server_mod._last_user_msg == "真实用户"
    assert server_mod._last_user_ts == server_mod._last_real_user_ts == 1000
