"""D cortex tests: DSTATE state, one-call Laya decisions, and persistence."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os

import httpx
import pytest

from gateway.decision import cortex
from gateway.decision.cortex import (
    DEFAULT_ACTION_OPTIONS,
    DEFAULT_REACTION_OPTIONS,
    DEFAULT_STRENGTH_OPTIONS,
    REACTION_MAX_TOKENS,
    DStateStore,
    active_decision,
    build_laya_state,
    clear_recent,
    decision_to_prompt,
    estimate_tokens,
    format_continuity,
    parse_dstate_block,
    passive_decision,
    strip_dstate_blocks,
    truncate_summary,
    update_from_round_text,
)
from gateway.decision.laya_router import LAYA_MODEL


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for name in ("PET_LAYA_ENABLED", "PET_LAYA_TIMEOUT", "PET_LAYA_ROLL_TIMEOUT", "PET_LAYA_URL"):
        monkeypatch.delenv(name, raising=False)
    # Every request below uses MockTransport and a non-user endpoint.
    monkeypatch.setenv("PET_LAYA_URL", "http://laya.test")
    monkeypatch.setenv("PET_DECISION_LOG", str(tmp_path / "decisions.jsonl"))
    clear_recent()
    yield
    clear_recent()


def _block(summary="我刚刚看了桌面。", **sections) -> str:
    return "<<<DSTATE>>>" + json.dumps(
        {"summary": summary, **sections}, ensure_ascii=False
    ) + "<<<DSTATEEND>>>"


def _choice(choice: str, ids: list[str]) -> dict:
    return {
        "type": "choice",
        "choice": choice,
        "probabilities": {option_id: float(option_id == choice) for option_id in ids},
        "answer_confidence": 1.0,
    }


def _laya_payload(
    strength="low", reaction="large", action="walk", actions=None
) -> dict:
    action_ids = actions or [option["id"] for option in DEFAULT_ACTION_OPTIONS]
    return {
        "answers": {
            "thinking": _choice(
                strength, [option["id"] for option in DEFAULT_STRENGTH_OPTIONS]
            ),
            "reaction": _choice(
                reaction, [option["id"] for option in DEFAULT_REACTION_OPTIONS]
            ),
            "action": _choice(action, action_ids),
        }
    }


def _transport(payload, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(json.loads(request.content.decode("utf-8")))
        if isinstance(payload, httpx.Response):
            return payload
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def test_dstate_parse_update_uses_last_block_and_strips_all():
    store = DStateStore()
    text = "之前" + _block("旧") + "中间" + _block("新") + "之后"

    parsed, cleaned = parse_dstate_block(text)
    assert parsed["summary"] == "新"
    assert cleaned == "之前中间之后"

    changed, cleaned = update_from_round_text(store, text)
    assert changed is True
    assert store.summary == "新"
    assert cleaned == "之前中间之后"
    assert store.updated_at.endswith("Z")


def test_invalid_json_and_missing_blocks_never_leak_markers():
    store = DStateStore()
    original = "你好"
    assert update_from_round_text(store, original) == (False, original)

    malformed = "可见<<<DSTATE>>>{bad<<<DSTATEEND>>>尾部"
    changed, cleaned = update_from_round_text(store, malformed)
    assert changed is False
    assert cleaned == "可见尾部"
    assert parse_dstate_block(malformed)[0] is None


def test_unterminated_trailing_block_is_stripped():
    text = "给用户看" + _block("完整") + "<<<DSTATE>>>{\"summary\":\"未完成"
    assert strip_dstate_blocks(text) == "给用户看"
    parsed, cleaned = parse_dstate_block(text)
    assert parsed["summary"] == "完整"
    assert cleaned == "给用户看"


def test_empty_or_invalid_sections_keep_previous_and_weights_are_clamped():
    store = DStateStore()
    previous = store.action_options
    changed, _ = update_from_round_text(
        store,
        _block(
            "总结",
            action_options=[],
            strength_options=[{"id": "deep", "label": "深", "weight": 1}],
            reaction_options=[
                {"id": "small", "label": "小", "weight": -2},
                {"id": "large", "label": "大", "weight": 4},
            ],
        ),
    )
    assert changed is True
    assert store.action_options == previous
    assert store.action_options is not previous
    assert store.strength_options == DEFAULT_STRENGTH_OPTIONS
    assert store.reaction_options == [
        {"id": "small", "label": "小", "weight": 0.0},
        {"id": "large", "label": "大", "weight": 1.0},
    ]


def test_default_actions_keep_other_and_separate_quiet_from_think():
    store = DStateStore()
    assert store.action_options == DEFAULT_ACTION_OPTIONS
    options = {option["id"]: option for option in store.action_options}
    assert {"quiet", "think", "other"} <= options.keys()
    assert options["think"]["label"] == "想"
    assert options["quiet"]["label"] != options["think"]["label"]


def test_action_options_are_capped_at_eight():
    store = DStateStore()
    options = [
        {"id": f"a{index}", "label": str(index), "weight": 1}
        for index in range(10)
    ]
    store.update({"summary": "x", "action_options": options})
    assert len(store.action_options) == 8
    assert store.action_options[-1]["id"] == "a7"


def test_token_estimate_and_truncate_ascii_cjk_and_unicode():
    assert estimate_tokens("hello world") == 2
    assert estimate_tokens("你好") == 2
    assert estimate_tokens("猫!") == 2  # one CJK + punctuation .25, rounded up
    assert estimate_tokens("😀") == 1
    assert truncate_summary("hello world", 1) == "hello"
    assert truncate_summary("你好世界", 2) == "你好"
    assert truncate_summary("😀😀", 0.25) == "😀"
    assert truncate_summary("abc", 0) == ""


def test_update_truncates_summary_to_1024_estimated_tokens():
    store = DStateStore()
    changed, _ = update_from_round_text(store, _block("猫" * 1100))
    assert changed is True
    assert len(store.summary) == 1024
    assert estimate_tokens(store.summary) == 1024


def test_build_laya_state_contains_summary_and_current_state():
    store = DStateStore()
    state = build_laya_state(
        store,
        {"user_text": "陪我聊聊", "is_proactive": True, "mood_index": 0.2},
    )
    assert "（还没有生命总结）" in state
    assert "陪我聊聊" in state
    assert "is_proactive：true" in state

    store.summary = "我刚刚散步回来"
    state = build_laya_state(store, {"user_text": "x" * 250, "idle_sec": 12})
    assert "我刚刚散步回来" in state
    assert "x" * 80 in state
    assert "x" * 81 not in state
    assert "idle_sec：12" in state


@pytest.mark.asyncio
async def test_passive_decision_one_request_and_complete_mapping():
    store = DStateStore()
    store.summary = "我在桌边观察用户"
    seen = []
    transport = _transport(_laya_payload(), seen)

    decision = await passive_decision(
        store,
        {"user_text": "过来", "is_proactive": True, "last_action": "quiet"},
        transport=transport,
    )

    assert len(seen) == 1
    body = seen[0]
    assert body["model"] == LAYA_MODEL
    assert "我在桌边观察用户" in body["state"]
    assert "过来" in body["state"]
    assert list(body["questions"]) == ["thinking", "reaction", "action"]
    assert body["questions"]["thinking"]["criteria"] == {
        option["id"]: option["label"] for option in DEFAULT_STRENGTH_OPTIONS
    }
    assert body["questions"]["reaction"]["criteria"] == {
        option["id"]: option["label"] for option in DEFAULT_REACTION_OPTIONS
    }
    assert body["questions"]["action"]["criteria"] == {
        option["id"]: option["label"] for option in DEFAULT_ACTION_OPTIONS
    }

    assert decision["decision_id"].startswith("d_")
    assert decision["source"] == "laya+prior"
    assert decision["strength"] == "low"
    assert decision["thinking"] is True
    assert decision["reasoning_effort"] == "low"
    assert decision["reaction"] == "large"
    assert decision["max_tokens"] == REACTION_MAX_TOKENS["large"] == 4096
    assert decision["action"] == "walk"
    assert decision["action_label"] == "走"
    assert decision["summary_used"] is True
    assert 0 <= decision["confidence"] <= 1
    assert store.last_decision == decision

    for qid in ("thinking", "reaction", "action"):
        question = decision["questions"][qid]
        assert question["choice"] in question["posterior"]
        assert question["shortlist"]
        assert set(question["posterior"]) == set(question["prior"])
        assert question["laya_choice"] is not None


@pytest.mark.asyncio
async def test_passive_user_turn_never_waits_on_laya():
    store = DStateStore()
    store.summary = "我在桌边观察用户"
    seen = []
    transport = _transport(_laya_payload(), seen)

    decision = await passive_decision(
        store,
        {"user_text": "过来", "is_proactive": False},
        transport=transport,
        timeout=5.0,
    )

    assert seen == []
    assert decision["source"] == "prior"
    assert decision["action"] == "quiet"
    assert decision["strength"] == "no_thinking"
    assert decision["reaction"] == "small"
    assert decision["reason"].startswith("user_turn_no_wait|safety_default")
    assert all(s["choice"] is None for s in decision["s_answers"])
    assert store.last_decision == decision

    active = await active_decision(
        store,
        {"user_text": "过来", "is_proactive": False},
        transport=transport,
    )
    assert len(seen) == 1
    assert active["source"] == "laya+prior"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["http_500", "timeout", "invalid_choice"])
async def test_laya_failures_use_safe_silence_not_prior_sampling(kind, monkeypatch):
    def forbidden_rng(*args, **kwargs):
        raise AssertionError("failure must not sample priors")

    monkeypatch.setattr(cortex.random, "Random", forbidden_rng)
    if kind == "http_500":
        transport = httpx.MockTransport(
            lambda request: httpx.Response(500, json={"error": "busy"})
        )
        timeout = None
    elif kind == "invalid_choice":
        payload = _laya_payload()
        payload["answers"]["action"]["choice"] = "not_an_option"
        transport = _transport(payload)
        timeout = None
    else:
        async def slow(request: httpx.Request) -> httpx.Response:
            await asyncio.sleep(0.1)
            return httpx.Response(200, json=_laya_payload())

        transport = httpx.MockTransport(slow)
        timeout = 0.01

    decision = await passive_decision(
        DStateStore(), transport=transport, timeout=timeout
    )
    assert decision["source"] == "prior"
    assert decision["strength"] == "no_thinking"
    assert decision["reaction"] == "small"
    assert decision["action"] == "quiet"
    assert decision["confidence"] == 0.0
    assert "safety_default" in decision["reason"]
    for question in decision["questions"].values():
        assert question["posterior"] == question["prior"]
        assert question["laya_choice"] is None
    assert "laya_" in decision["reason"]
    if kind == "timeout":
        await cortex._INFLIGHT[asyncio.get_running_loop()]


@pytest.mark.asyncio
async def test_active_decision_custom_actions_context_and_no_store_mutation():
    store = DStateStore()
    store.summary = "保持现状"
    before = store.to_dict()
    custom = [
        {"id": "dance", "label": "跳舞", "weight": 0.7},
        {"id": "sit", "label": "坐下", "weight": 0.3},
    ]
    seen = []
    payload = _laya_payload(
        strength="no_thinking", reaction="small", action="dance",
        actions=["dance", "sit"],
    )

    decision = await active_decision(
        store,
        action_options=custom,
        context="用户明确邀请我跳舞",
        transport=_transport(payload, seen),
    )

    assert decision["action"] == "dance"
    assert decision["action_label"] == "跳舞"
    assert decision["thinking"] is False
    assert decision["reasoning_effort"] is None
    assert decision["max_tokens"] == 2048
    assert seen[0]["questions"]["action"]["criteria"] == {
        "dance": "跳舞", "sit": "坐下"
    }
    assert "用户明确邀请我跳舞" in seen[0]["state"]
    assert store.to_dict() == before


@pytest.mark.asyncio
async def test_disabled_laya_never_calls_http(monkeypatch):
    monkeypatch.setenv("PET_LAYA_ENABLED", "0")

    def forbidden(request):
        raise AssertionError("disabled Laya must not receive HTTP")

    decision = await passive_decision(
        DStateStore(), transport=httpx.MockTransport(forbidden)
    )
    assert decision["source"] == "prior"
    assert decision["reason"].startswith("laya_disabled")


def test_store_save_load_roundtrip_and_round_update_persists(tmp_path):
    path = tmp_path / "themes" / "cat" / "d-state.json"
    store = DStateStore(path)
    changed, cleaned = update_from_round_text(
        store,
        "回答" + _block(
            "我记住了这次对话",
            action_options=[{"id": "look", "label": "看", "weight": 1}],
        ),
    )
    assert changed is True
    assert cleaned == "回答"
    assert path.exists()

    loaded = DStateStore(path)
    assert loaded.summary == "我记住了这次对话"
    assert loaded.action_options == [{"id": "look", "label": "看", "weight": 1.0}]
    assert loaded.strength_options == DEFAULT_STRENGTH_OPTIONS
    assert loaded.updated_at == store.updated_at

    loaded.last_decision = {"decision_id": "d_test"}
    loaded.save()
    assert DStateStore(path).last_decision == {"decision_id": "d_test"}


def test_corrupt_store_is_tolerated(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("not json", encoding="utf-8")
    store = DStateStore(path)
    assert store.summary == ""
    assert store.action_options == DEFAULT_ACTION_OPTIONS


def test_decision_to_prompt_has_all_fields_and_slow_tool_hint():
    prompt = decision_to_prompt(
        {
            "strength": "no_thinking",
            "reaction": "medium",
            "max_tokens": 3072,
            "action": "look",
            "action_label": "看",
            "summary": "绝不能出现在提示里",
        }
    )
    assert "【这一轮你的底层直觉】" in prompt
    assert "思考强度：不思考" in prompt
    assert "持续反应：中（max_tokens=3072）" in prompt
    assert "默认动作：看" in prompt
    assert "pet_enter_slow" in prompt
    assert "用户直接说话必须回应" in prompt
    assert "绝不能出现在提示里" not in prompt


@pytest.mark.asyncio
async def test_decision_log_hashes_summary_instead_of_logging_it(tmp_path):
    path = tmp_path / "cortex.jsonl"
    os.environ["PET_DECISION_LOG"] = str(path)
    store = DStateStore()
    store.summary = "私密生命总结"
    decision = await passive_decision(store, transport=_transport(_laya_payload()))
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])

    assert record["type"] == "cortex_decision"
    assert record["decision_id"] == decision["decision_id"]
    assert record["summary_sha256"] == hashlib.sha256(
        store.summary.encode("utf-8")
    ).hexdigest()
    assert "私密生命总结" not in json.dumps(record, ensure_ascii=False)


# ── S questions: the model's natural prompts for D, with weight layers ──


def test_s_questions_parse_and_persist(tmp_path):
    store = DStateStore()
    text = _block(
        "我在想。",
        questions=[{"q": "我要说话吗？", "options": ["yes", "no"], "weight": "light"}],
    )
    changed, _ = update_from_round_text(store, text)
    assert changed is True
    assert store.questions == [
        {"q": "我要说话吗？", "options": ["yes", "no"], "weight": "light"}
    ]
    path = tmp_path / "d.json"
    store.path = path
    store.save()
    loaded = DStateStore(path)
    assert loaded.questions == store.questions


def test_s_questions_invalid_weight_defaults_to_light():
    store = DStateStore()
    update_from_round_text(
        store,
        _block("x", questions=[{"q": "q?", "options": ["a", "b"], "weight": "bogus"}]),
    )
    assert store.questions[0]["weight"] == "light"


def test_reflex_is_only_kept_for_head_turn_questions():
    store = DStateStore()
    update_from_round_text(
        store,
        _block(
            "x",
            questions=[
                {"q": "往哪边看？", "options": ["左", "右"], "weight": "reflex"},
                {"q": "look?", "options": ["Left", "RIGHT"], "weight": "reflex"},
                {"q": "要眨眼吗？", "options": ["要", "不要"], "weight": "reflex"},
            ],
        ),
    )
    assert [q["weight"] for q in store.questions] == ["reflex", "reflex", "light"]


@pytest.mark.asyncio
async def test_s_questions_asked_and_answered_without_breaking_fixed_three():
    store = DStateStore()
    store.questions = [{"q": "我要说话吗？", "options": ["yes", "no"], "weight": "light"}]
    payload = _laya_payload()
    payload["answers"]["s0"] = _choice("yes", ["yes", "no"])
    seen = []
    decision = await passive_decision(store, transport=_transport(payload, seen))
    assert "s0" in seen[0]["questions"]
    assert decision["action"] in [o["id"] for o in DEFAULT_ACTION_OPTIONS]
    assert decision["s_answers"][0]["choice"] == "yes"
    assert decision["s_answers"][0]["weight"] == "light"


@pytest.mark.asyncio
async def test_laya_omitting_s_question_does_not_break_decision():
    store = DStateStore()
    store.questions = [{"q": "我要说话吗？", "options": ["yes", "no"], "weight": "heavy"}]
    decision = await passive_decision(store, transport=_transport(_laya_payload()))
    assert decision["source"] == "laya+prior"
    assert decision["s_answers"][0]["choice"] is None


def test_decision_to_prompt_shows_s_weights():
    decision = {
        "strength": "no_thinking",
        "reaction": "medium",
        "action": "look",
        "action_label": "看",
        "s_answers": [
            {"q": "我要说话吗？", "weight": "heavy", "choice": "yes", "confidence": 0.9},
            {"q": "往哪边看？", "weight": "reflex", "choice": "左", "confidence": 0.8},
        ],
    }
    prompt = decision_to_prompt(decision)
    assert "我要说话吗？" in prompt and "你必须自己确认" in prompt
    assert "往哪边看？" in prompt and "反射没执行" in prompt
    assert "身体已经转向" not in prompt

    decision["s_answers"][1]["applied"] = "left"
    assert "身体已经转向左边" in decision_to_prompt(decision)


def test_system_continuity_defaults_and_real_response_marker():
    store = DStateStore()
    assert store.last_user == store.last_round == {}
    assert store.to_dict()["last_user"] == store.to_dict()["last_round"] == {}

    store.note_user("问" * 501, "user-1", 100.25)
    assert store.last_user == {
        "text": "问" * 500,
        "round_id": "user-1",
        "received_at": 100.25,
        "responded_at": None,
    }
    store.record_round("user-1", 101.0, 104.5, False, "答" * 501, "想" * 501, [])
    assert store.last_user["responded_at"] == 104.5
    assert store.last_round == {
        "round_id": "user-1",
        "started_at": 101.0,
        "finished_at": 104.5,
        "proactive": False,
        "spoken": "答" * 500,
        "inner": "想" * 500,
        "receipts": [],
    }
    store.note_user("下一问", "user-2", 110.0)
    assert store.last_user["responded_at"] is None


@pytest.mark.parametrize("proactive, spoken", [(False, ""), (False, " \n\t"), (True, "有发言")])
def test_round_without_user_facing_response_does_not_mark_answered(proactive, spoken):
    store = DStateStore()
    store.note_user("过来", "user-1", 100.0)
    store.record_round(
        "user-1", 101.0, 104.0, proactive, spoken, "已经回答了",
        [{"status": "ok", "applied": True, "summary": "已转头"}],
    )
    assert store.last_user["responded_at"] is None
    assert store.last_round["finished_at"] == 104.0
    assert "尚未回答" in format_continuity(store, 110.0, proactive)


def test_late_old_round_cannot_confirm_a_new_user_request():
    store = DStateStore()
    store.note_user("旧问题", "old", 100.0)
    store.note_user("新问题", "new", 105.0)
    before = store.to_dict()["last_user"]
    store.record_round("old", 101.0, 106.0, False, "旧回答", "旧心声", [])
    assert store.last_user == before
    store.record_round("new", 107.0, 109.0, False, "新回答", "", [])
    assert store.last_user["responded_at"] == 109.0
    store.record_round("old", 101.0, 110.0, False, "迟到旧回答", "", [])
    assert store.last_user["text"] == "新问题"
    assert store.last_user["responded_at"] == 109.0


def test_round_receipts_are_bounded_and_snapshots_do_not_mutate_facts():
    store = DStateStore()
    receipts = [{"status": "ok", "applied": True, "summary": str(i)} for i in range(10)]
    store.record_round("r", 100.0, 101.0, True, "", "", receipts)
    assert store.last_round["receipts"] == receipts[-8:]
    receipts[-8]["status"] = "error"
    snapshot = store.to_dict()
    snapshot["last_round"]["receipts"][1]["applied"] = False
    snapshot["last_round"]["spoken"] = "虚假发言"
    assert store.last_round["receipts"][0]["status"] == "ok"
    assert store.last_round["receipts"][1]["applied"] is True
    assert store.last_round["spoken"] == ""


@pytest.mark.parametrize("populated", [False, True])
def test_model_dstate_cannot_overwrite_system_continuity(populated):
    store = DStateStore()
    if populated:
        store.note_user("真问题", "real", 100.0)
        store.record_round("real", 101.0, 104.0, False, "真回答", "真心声", [])
    before = store.to_dict()
    forged = {
        "last_user": {"text": "假问题", "round_id": "fake", "received_at": 999.0, "responded_at": 999.0},
        "last_round": {"round_id": "fake", "receipts": [{"status": "ok", "applied": True}]},
    }
    store.update({"summary": "模型声称已执行", **forged})
    changed, _ = update_from_round_text(store, _block("另一个主观总结", **forged))
    assert changed is True
    assert store.summary == "另一个主观总结"
    assert store.last_user == before["last_user"]
    assert store.last_round == before["last_round"]


@pytest.mark.parametrize("spoken, answered_at", [("真回答", 104.0), ("", None)])
def test_system_continuity_persists_with_response_status(tmp_path, spoken, answered_at):
    path = tmp_path / "facts.json"
    store = DStateStore(path)
    store.note_user("真实问题", "real", 100.0)
    store.record_round(
        "real", 101.0, 104.0, False, spoken, "真实心声",
        [{"status": "ok", "applied": False, "summary": "仍待执行"}],
    )
    store.save()
    loaded = DStateStore(path)
    assert loaded.to_dict() == store.to_dict()
    assert loaded.last_user["responded_at"] == answered_at
    loaded.update({"summary": "我幻想已经移动"})
    loaded.save()
    assert DStateStore(path).last_round == store.last_round


@pytest.mark.parametrize("invalid", [None, [], "fake", {}, {"received_at": True}, {"finished_at": "yesterday"}])
def test_loading_invalid_continuity_records_keeps_empty_defaults(tmp_path, invalid):
    path = tmp_path / "invalid-facts.json"
    path.write_text(json.dumps({"last_user": invalid, "last_round": invalid}), encoding="utf-8")
    store = DStateStore(path)
    assert store.last_user == store.last_round == {}


def test_loading_continuity_bounds_fields_without_inferring_a_response(tmp_path):
    path = tmp_path / "bounded-facts.json"
    receipts = [{"status": "ok", "applied": True, "summary": str(i)} for i in range(10)]
    path.write_text(json.dumps({
        "last_user": {
            "text": "问" * 600, "round_id": "r", "received_at": 100.0,
            "responded_at": None, "extra": "ignored",
        },
        "last_round": {
            "round_id": "r", "started_at": 101.0, "finished_at": 104.0,
            "proactive": False, "spoken": "答" * 600, "inner": "想" * 600,
            "receipts": receipts, "extra": "ignored",
        },
    }), encoding="utf-8")
    store = DStateStore(path)
    assert len(store.last_user["text"]) == 500
    assert len(store.last_round["spoken"]) == len(store.last_round["inner"]) == 500
    assert store.last_round["receipts"] == receipts[-8:]
    assert store.last_user["responded_at"] is None
    assert "extra" not in store.last_user and "extra" not in store.last_round


@pytest.mark.parametrize("invalid_time", [True, "100", float("nan"), float("inf")])
def test_loading_invalid_fact_timestamps_does_not_invent_an_epoch(tmp_path, invalid_time):
    path = tmp_path / "invalid-times.json"
    path.write_text(json.dumps({
        "last_user": {"text": "问", "round_id": "r", "received_at": invalid_time},
        "last_round": {
            "round_id": "r", "started_at": 100.0, "finished_at": invalid_time,
            "proactive": False, "spoken": "答", "inner": "", "receipts": [],
        },
    }), encoding="utf-8")
    store = DStateStore(path)
    assert store.last_user == store.last_round == {}


@pytest.mark.parametrize("receipts", [None, "fake", {"status": "ok"}, [None, "fake", {"status": "ok", "applied": True}]])
def test_loading_malformed_receipt_and_text_fields_is_safe(tmp_path, receipts):
    path = tmp_path / "malformed-fields.json"
    path.write_text(json.dumps({
        "last_user": {"text": "问", "round_id": "r", "received_at": 100.0, "responded_at": True},
        "last_round": {
            "round_id": "r", "started_at": 101.0, "finished_at": 104.0,
            "proactive": False, "spoken": [], "inner": {}, "receipts": receipts,
        },
    }), encoding="utf-8")
    store = DStateStore(path)
    assert store.last_user["responded_at"] is None
    assert store.last_round["spoken"] == store.last_round["inner"] == ""
    expected = [{"status": "ok", "applied": True}] if isinstance(receipts, list) else []
    assert store.last_round["receipts"] == expected
    assert "尚未回答" in format_continuity(store, 110.0, False)


def test_continuity_is_isolated_between_theme_stores(tmp_path):
    cat = DStateStore(tmp_path / "cat.json")
    dog = DStateStore(tmp_path / "dog.json")
    cat.note_user("猫的话题", "cat-round", 100.0)
    cat.record_round("cat-round", 101.0, 104.0, False, "猫的回答", "猫的心声", [])
    dog.note_user("狗的话题", "dog-round", 110.0)
    dog.record_round("dog-round", 111.0, 114.0, True, "", "狗的心声", [])
    cat.save()
    dog.save()
    assert DStateStore(cat.path).last_user["responded_at"] == 104.0
    assert DStateStore(dog.path).last_user["responded_at"] is None
    assert "狗的话题" not in format_continuity(cat, 120.0, True)
    assert "猫的话题" not in format_continuity(dog, 120.0, True)


def test_format_continuity_uses_real_elapsed_time_speech_and_receipts():
    store = DStateStore()
    store.note_user("现在怎么办", "r-real", 1700000000.0)
    store.record_round(
        "r-real", 1700000001.0, 1700000005.0, False, "我先等一下", "想走但尚未执行",
        [
            {"status": "ok", "applied": True, "summary": "身体确认已转头"},
            {"status": "ok", "applied": False, "summary": "行走仍待执行"},
            {"status": "error", "applied": True, "summary": "感知失败"},
        ],
    )
    prompt = format_continuity(store, 1700000025.5, True)
    assert prompt.startswith("【实时经历（系统事实）】")
    assert "本轮没有新的用户消息；历史对话已发生，不要把旧话当刚收到的问题" in prompt
    assert "25.5秒前收到；已回答" in prompt
    assert "20.5秒前结束" in prompt
    assert "round_id=r-real" in prompt and "完成时间（epoch）=1700000005.0" in prompt
    assert '当时实际说出："我先等一下"' in prompt
    assert '当时真实心声记录："想走但尚未执行"' in prompt
    assert "回执1：成功（已完成）" in prompt and "身体确认已转头" in prompt
    assert "回执2：未完成" in prompt and 'applied=false' in prompt
    assert "回执3：未完成" in prompt and 'status="error"' in prompt
    assert "不要在正文复述时间和轮次ID" in prompt
    assert "summary/S 判断是主观直觉" in prompt
    assert "本轮有新的用户输入。" in format_continuity(store, 1700000025.5, False)


@pytest.mark.parametrize("status, applied", [("pending", True), ("ok", None), ("ok", "true"), ("error", False)])
def test_receipt_without_confirmed_completion_is_not_success(status, applied):
    store = DStateStore()
    store.record_round("r", 100.0, 104.0, True, "", "", [
        {"status": status, "applied": applied, "summary": "模型说已经移动"},
    ])
    prompt = format_continuity(store, 110.0, True)
    assert "回执1：未完成（不能当作已执行）" in prompt
    assert "成功（已完成）" not in prompt
    assert "完成回执：0成功，1未完成。" in build_laya_state(store, {"now": 110.0})


def test_successful_tool_receipt_is_not_misreported_as_motor_failure():
    store = DStateStore()
    store.record_round("r", 100.0, 104.0, True, "", "", [
        {"name": "capture_screen", "kind": "tool", "status": "ok", "summary": "工具返回截图"},
    ])
    prompt = format_continuity(store, 110.0, True)
    assert "成功（工具已返回，不等于身体动作）" in prompt
    assert "未完成（不能当作已执行）" not in prompt


def test_latest_failure_receipt_is_retained():
    store = DStateStore()
    receipts = [{"name": f"step{i}", "status": "ok", "applied": True} for i in range(9)]
    receipts.append({"name": "walk", "status": "error", "applied": False, "summary": "移动失败"})
    store.record_round("r", 100.0, 104.0, True, "", "", receipts)
    assert len(store.last_round["receipts"]) == 8
    assert store.last_round["receipts"][-1]["status"] == "error"
    assert "移动失败" in format_continuity(store, 110.0, True)


def test_continuity_without_receipts_never_promotes_summary_or_decision_to_facts():
    store = DStateStore()
    store.summary = "幻想已看见屏幕并走到了窗边"
    store.last_decision = {"action": "walk"}
    store.record_round("r", 100.0, 104.0, True, "", "想象去窗边", [])
    prompt = format_continuity(store, 110.0, True)
    assert "上一轮没有收到动作/感知完成回执；动作叙述或summary不是执行证据" in prompt
    assert "幻想已看见屏幕并走到了窗边" not in prompt
    assert "walk" not in prompt
    assert "（无可见发言）" in prompt
    assert "不能推断用户离开多久" in prompt
    empty = format_continuity(DStateStore(), 110.0, True)
    assert "尚无已结束轮次记录" in empty
    assert "秒前" not in empty


def test_build_laya_state_adds_compact_real_facts_without_losing_current_fields(monkeypatch):
    monkeypatch.setattr("gateway.decision.cortex.time.time", lambda: 999.0)
    store = DStateStore()
    store.summary = "主观总结仍在"
    store.note_user("真实请求", "r", 100.0)
    store.record_round("r", 101.0, 104.0, False, "实际回答", "真实心声", [])
    store.last_decision = {"action": "imagined_walk"}
    state = build_laya_state(store, {"now": 120.0, "is_proactive": True, "idle_sec": 12, "last_action": "walk"})
    facts = state.split("【实时经历（系统事实）】", 1)[1].split("生命总结：", 1)[0]
    assert state.index("【实时经历（系统事实）】") < state.index("主观总结仍在")
    assert "20.0秒前收到，已回答" in facts and "真实请求" in facts
    assert "上一轮16.0秒前结束" in facts
    assert "实际回答" in facts and "真实心声" in facts
    assert "本轮没有新的用户消息" in facts
    assert "summary不是执行证据" in facts
    assert "is_proactive：true" in state and "idle_sec：12" in state
    assert "last_action：walk（动作选择，非执行证据）" in state
    assert "imagined_walk" not in state
    assert 100 <= len(facts) <= 250
    assert "上一轮895.0秒前结束" in build_laya_state(store, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("passive", [False, True])
async def test_decisions_do_not_mutate_or_confirm_system_continuity(tmp_path, passive):
    store = DStateStore(tmp_path / "decision-facts.json")
    store.note_user("还没回应的请求", "pending", 100.0)
    store.record_round("previous", 98.0, 99.0, True, "", "真实心声", [])
    before = store.to_dict()
    decide = passive_decision if passive else active_decision
    await decide(store, {"now": 110.0}, transport=_transport(_laya_payload()))
    assert store.last_user == before["last_user"]
    assert store.last_round == before["last_round"]
    assert store.last_user["responded_at"] is None
    if passive:
        loaded = DStateStore(store.path)
        assert loaded.last_user == before["last_user"]
        assert loaded.last_round == before["last_round"]
    else:
        assert store.to_dict() == before
        assert not store.path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_active,second_active", [(False, False), (False, True), (True, False), (True, True)])
async def test_timeout_keeps_shared_gate_until_http_finishes_without_retry(first_active, second_active):
    store = DStateStore()
    store.questions = [{"q": "旧S？", "options": ["旧甲", "旧乙"], "weight": "light"}]
    finish = asyncio.Event()
    seen = []

    class TrackedTransport(httpx.MockTransport):
        closed = False

        async def aclose(self):
            self.closed = True
            await super().aclose()

    async def slow(request):
        assert request.extensions["timeout"]["read"] == 30.0
        body = json.loads(request.content)
        seen.append(body)
        await finish.wait()
        assert not transport.closed, "short-lived client must outlive the waiter"
        payload = _laya_payload()
        payload["answers"]["s0"] = _choice("旧甲", ["旧甲", "旧乙"])
        return httpx.Response(200, json=payload)

    transport = TrackedTransport(slow)
    first_call = active_decision if first_active else passive_decision
    first = await first_call(store, transport=transport, timeout=0.01)
    assert first["reason"].startswith("laya_timeout|safety_default")
    assert first["s_answers"][0]["choice"] is None
    loop = asyncio.get_running_loop()
    gate = cortex._INFLIGHT[loop]
    assert not gate.done() and not transport.closed
    clear_recent()
    assert cortex._INFLIGHT[loop] is gate  # config reset cannot cancel inference

    store.questions = [{"q": "新S？", "options": ["新甲", "新乙"], "weight": "heavy"}]
    second_seen = []
    second_call = active_decision if second_active else passive_decision
    async with httpx.AsyncClient(transport=_transport(_laya_payload(), second_seen)) as client:
        second = await asyncio.wait_for(second_call(store, client=client), timeout=0.1)
        assert not client.is_closed
    assert second["reason"].startswith("laya_busy|safety_default")
    assert second["s_answers"] == [{"q": "新S？", "weight": "heavy", "choice": None, "confidence": 0.0}]
    assert len(seen) == 1 and second_seen == []  # no queuing or same-round retry
    saved_decision = store.last_decision
    recent_ids = list(cortex._RECENT)
    finish.set()
    await gate
    assert transport.closed
    assert loop not in cortex._INFLIGHT
    assert store.last_decision is saved_decision
    assert list(cortex._RECENT) == recent_ids  # late S answer was not remembered
    assert first["s_answers"][0]["choice"] is None
    assert second["s_answers"][0]["choice"] is None

    payload = _laya_payload(action="quiet")
    payload["answers"]["s0"] = _choice("新乙", ["新甲", "新乙"])
    fresh = await passive_decision(store, transport=_transport(payload, second_seen))
    assert fresh["source"] == "laya+prior"
    assert fresh["s_answers"][0]["choice"] == "新乙"
    assert len(second_seen) == 1 and loop not in cortex._INFLIGHT


def test_gate_is_not_reused_across_distinct_async_loops():
    async def timed_out():
        async def never_finishes(request):
            await asyncio.Event().wait()

        result = await passive_decision(
            DStateStore(), transport=httpx.MockTransport(never_finishes), timeout=0.01,
        )
        assert "laya_timeout" in result["reason"]
        return asyncio.get_running_loop()

    old_loop = asyncio.run(timed_out())  # shutdown cancels only this loop's work
    assert old_loop not in cortex._INFLIGHT
    result = asyncio.run(passive_decision(DStateStore(), transport=_transport(_laya_payload())))
    assert result["source"] == "laya+prior"
    assert not cortex._INFLIGHT


@pytest.mark.asyncio
async def test_hard_http_deadline_releases_gate_and_owned_client(monkeypatch):
    monkeypatch.setattr(cortex, "LAYA_HTTP_DEADLINE", 0.03)
    cancelled = asyncio.Event()

    async def never_finishes(request):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    result = await passive_decision(
        DStateStore(), transport=httpx.MockTransport(never_finishes), timeout=0.005,
    )
    assert "laya_timeout" in result["reason"]
    loop = asyncio.get_running_loop()
    gate = cortex._INFLIGHT[loop]
    assert await gate == (None, "laya_timeout")
    assert cancelled.is_set() and loop not in cortex._INFLIGHT
    result = await passive_decision(DStateStore(), transport=_transport(_laya_payload()))
    assert result["source"] == "laya+prior"


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_another_http_inference():
    started, finish = asyncio.Event(), asyncio.Event()

    async def slow(request):
        started.set()
        await finish.wait()
        return httpx.Response(200, json=_laya_payload())

    store = DStateStore()
    waiter = asyncio.create_task(passive_decision(store, transport=httpx.MockTransport(slow)))
    await started.wait()
    gate = cortex._INFLIGHT[asyncio.get_running_loop()]
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert not gate.done() and store.last_decision is None
    busy = await active_decision(store, transport=_transport(_laya_payload()))
    assert "laya_busy" in busy["reason"]
    finish.set()
    await gate
    assert store.last_decision is None


@pytest.mark.asyncio
async def test_finished_callback_consumes_late_task_exception(monkeypatch):
    finish, done = asyncio.Event(), asyncio.Event()

    async def unexpected(*args):
        await finish.wait()
        raise RuntimeError("background failure")

    monkeypatch.setattr(cortex, "_laya_http", unexpected)
    result = await passive_decision(DStateStore(), transport=_transport(_laya_payload()), timeout=0.01)
    assert "laya_timeout" in result["reason"]
    loop = asyncio.get_running_loop()
    gate = cortex._INFLIGHT[loop]
    gate.add_done_callback(lambda task: done.set())
    finish.set()
    await done.wait()
    assert loop not in cortex._INFLIGHT
    assert not gate._log_traceback, "exception must be consumed without a waiting caller"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["disabled", "http_503", "invalid_json", "web_harness", "connect_error"])
async def test_proactive_failure_is_explicit_safe_silence_even_with_no_quiet_weight(monkeypatch, kind):
    store = DStateStore()
    store.action_options = [{"id": "speak", "label": "说", "weight": 1.0}]
    store.strength_options = [{"id": "low", "label": "low", "weight": 1.0}]
    store.reaction_options = [{"id": "large", "label": "大", "weight": 1.0}]
    store.questions = [{"q": "走吗？", "options": ["走", "不走"], "weight": "light"}]
    seen = []
    if kind == "disabled":
        monkeypatch.setenv("PET_LAYA_ENABLED", "0")

    def handler(request):
        seen.append(request)
        if kind == "connect_error":
            raise httpx.ConnectError("unavailable", request=request)
        if kind == "invalid_json":
            return httpx.Response(200, content=b"not-json")
        return httpx.Response(503 if kind == "http_503" else 200, json={"status": "ok"})

    def forbidden_rng(*args, **kwargs):
        raise AssertionError("proactive failure must not roll priors")

    monkeypatch.setattr(cortex.random, "Random", forbidden_rng)
    result = await passive_decision(store, {"is_proactive": True}, transport=httpx.MockTransport(handler))
    assert (result["action"], result["strength"], result["reaction"]) == ("quiet", "no_thinking", "small")
    assert result["source"] == "prior" and "safety_default" in result["reason"]
    assert result["thinking"] is False and result["confidence"] == 0
    assert result["s_answers"][0]["choice"] is None
    assert len(seen) == (0 if kind == "disabled" else 1)


@pytest.mark.asyncio
async def test_active_failure_retains_legal_custom_actions_without_sampling():
    custom = [{"id": "sit", "label": "坐", "weight": 0.0}, {"id": "dance", "label": "舞", "weight": 1.0}]
    result = await active_decision(
        DStateStore(), action_options=custom,
        transport=_transport(httpx.Response(503, json={"error": "busy"})),
    )
    assert result["action"] == "sit"  # deterministic legal default, not weighted dice
    assert result["strength"] == "no_thinking" and result["reaction"] == "small"
    assert result["source"] == "prior" and "safety_default:legal_custom_action" in result["reason"]


@pytest.mark.asyncio
async def test_normal_sampling_keeps_all_positive_actions_including_quiet():
    payload = _laya_payload()
    ids = [o["id"] for o in DEFAULT_ACTION_OPTIONS]
    payload["answers"]["action"] = {
        "choice": "speak", "probabilities": {option_id: 1.0 / len(ids) for option_id in ids},
    }
    picked = set()
    for i in range(60):
        result = await passive_decision(DStateStore(), {"user_text": f"状态{i}"}, transport=_transport(payload))
        question = result["questions"]["action"]
        assert set(question["shortlist"]) == set(ids)
        assert question["posterior"]["quiet"] > 0
        assert question["posterior"]["think"] > 0
        picked.add(result["action"])
    assert {"quiet", "think", "other"} <= picked
    assert picked == set(ids)


@pytest.mark.asyncio
async def test_compact_state_preserves_all_s_prompts_options_and_named_real_facts():
    store = DStateStore()
    store.update({"summary": "猫" * 1100})
    store.note_user("问" * 200, "r", 100.0)
    store.record_round("r", 101.0, 104.0, False, "答" * 200, "想" * 200, [
        {"name": "pet_turn_head", "status": "ok", "applied": True, "summary": "真实转头"},
        {"name": "pet_walk_to", "status": "ok", "applied": False, "summary": "未走"},
        {"name": "capture_screen", "status": "error", "applied": False, "summary": "未观察"},
    ])
    store.questions = [
        {"q": f"第{i}题保持完整" + "题" * 70, "options": ["完整甲" + "甲" * 30, "完整乙" + "乙" * 30], "weight": "heavy"}
        for i in range(2)
    ]
    payload = _laya_payload()
    for i, q in enumerate(store.questions):
        payload["answers"][f"s{i}"] = _choice(q["options"][1], q["options"])
    seen = []
    result = await active_decision(store, {
        "now": 120.0, "is_proactive": True, "user_text": "新" * 200,
        "idle_sec": 20, "provider_type": "web", "last_action": "walk",
    }, context="境" * 200, transport=_transport(payload, seen))
    state = seen[0]["state"]
    assert state.startswith("【实时经历（系统事实）】")
    assert state.index("已回答") < state.index("生命总结：")
    assert "20.0秒前收到，已回答" in state and "上一轮16.0秒前结束" in state
    assert "答" * 16 in state and "答" * 17 not in state
    assert "想" * 16 in state and "想" * 17 not in state
    assert "成功回执：pet_turn_head" in state
    assert "未完成回执：pet_walk_to、capture_screen" in state
    assert "当前能力：无本轮观察回执" in state
    assert "last_action：walk（动作选择，非执行证据）" in state
    assert "is_proactive：true" in state and "provider_type：web" in state
    assert "新" * 80 in state and "新" * 81 not in state
    summary = state.split("生命总结：\n", 1)[1].split("\n当前状态：", 1)[0]
    context = state.split("\ncontext：", 1)[1]
    assert estimate_tokens(summary) <= 64 and estimate_tokens(context) <= 64
    assert estimate_tokens(store.summary) == 1024
    assert list(seen[0]["questions"]) == ["thinking", "reaction", "action", "s0", "s1"]
    for i, q in enumerate(store.questions):
        sent = seen[0]["questions"][f"s{i}"]
        assert sent["instructions"] == q["q"]
        assert sent["criteria"] == {o: o for o in q["options"]}
        assert result["s_answers"][i]["choice"] == q["options"][1]


@pytest.mark.asyncio
async def test_questions_snapshot_cannot_be_relabelled_by_update_during_http():
    store = DStateStore()
    store.questions = [{"q": "原问题", "options": ["甲", "乙"], "weight": "light"}]

    async def handler(request):
        store.questions = [{"q": "下一轮问题", "options": ["新甲", "新乙"], "weight": "heavy"}]
        payload = _laya_payload()
        payload["answers"]["s0"] = _choice("乙", ["甲", "乙"])
        return httpx.Response(200, json=payload)

    result = await active_decision(store, transport=httpx.MockTransport(handler))
    assert result["s_answers"] == [{"q": "原问题", "weight": "light", "choice": "乙", "confidence": 1.0}]
    assert store.questions[0]["q"] == "下一轮问题"
