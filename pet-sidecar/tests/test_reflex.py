"""reflex tests -- no real LM Studio involved.

Pins the gateway contract:
  * config precedence: env > providers.json "reflex" > defaults;
  * env/providers disables; missing/malformed providers.json → defaults;
  * the prefill + json_schema request contract (the lfm3 production lever);
  * prefill continuation parsing, clamping to the tempo tier, coercion;
  * every failure path (timeout / HTTP error / bad JSON / invalid fields)
    falls back to quiet and never raises;
  * disabled config → fallback without any request;
  * user prompt is compact Chinese lines, never a raw JSON dump.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from gateway.decision import reflex
from gateway.decision.reflex import (
    DEFAULT_REFLEX_MAX_TOKENS,
    DEFAULT_REFLEX_URL,
    TEMPO_RANGE_S,
    ReflexConfig,
    build_user_prompt,
    judge,
    load_reflex_config,
)

# TEMPO_RANGE_S must keep mirroring server.py _TEMPO_SPECS["interval_s"]
# (full parity test exists at wiring time; this keeps the constant honest).
def test_tempo_ranges_match_server_tempo_specs():
    assert TEMPO_RANGE_S == {"slow": (240, 360), "fast": (90, 150), "ultra": (50, 70)}


_NIGHT_STATE = {
    "summary": "深夜 02:00，主人已睡，房间里只有呼吸声",
    "tempo_mode": "slow",
    "new_events": 0,
    "user_present": False,
    "idle_s": 1800.0,
    "mood": "平静",
    "recent_thoughts": ["好安静", "有点困"],
}


def _config(**overrides) -> ReflexConfig:
    values = {
        "url": "http://127.0.0.1:1234/v1",
        "model": "lfm3-1.7b",
        "api_key": "lm-studio",
        "prefill": "{",
        "max_tokens": 128,
        "timeout_s": 4.0,
        "enabled": True,
    }
    values.update(overrides)
    return ReflexConfig(**values)


def _content_response(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        },
    )


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    # Never let the developer's real ~/.pet/providers.json leak into config
    # tests; never let real PET_REFLEX_* env leak into the resolution order.
    monkeypatch.setattr(
        reflex, "_default_providers_path", lambda: tmp_path / "providers.json"
    )
    for name in ("PET_REFLEX_URL", "PET_REFLEX_MODEL", "PET_REFLEX_ENABLED"):
        monkeypatch.delenv(name, raising=False)
    yield


# ── Config resolution ────────────────────────────────────────────────────────


def test_defaults_when_providers_json_missing(tmp_path):
    config = load_reflex_config(tmp_path / "nope.json")
    assert config.url == DEFAULT_REFLEX_URL
    assert config.model == "lfm3-1.7b"
    assert config.enabled is True
    assert config.prefill == "{"
    assert config.max_tokens == 128
    assert config.timeout_s == pytest.approx(4.0)
    assert config.api_key == "lm-studio"


def test_env_wins_over_providers_json(tmp_path):
    (tmp_path / "providers.json").write_text(
        json.dumps(
            {
                "reflex": {
                    "baseUrl": "http://127.0.0.1:9999/v1",
                    "model": "file-model",
                    "enabled": True,
                }
            }
        ),
        encoding="utf-8",
    )
    config = load_reflex_config(
        tmp_path / "providers.json",
        env={
            "PET_REFLEX_URL": "http://127.0.0.1:18888/v1/",
            "PET_REFLEX_MODEL": "env-model",
        },
    )
    assert config.url == "http://127.0.0.1:18888/v1"  # trailing "/" stripped
    assert config.model == "env-model"
    assert config.enabled is True


def test_providers_json_wins_over_defaults(tmp_path):
    (tmp_path / "providers.json").write_text(
        json.dumps(
            {
                "providers": {"local": {"apiKey": "x"}},  # other sections ignored
                "reflex": {
                    "baseUrl": "http://127.0.0.1:9999/v1",
                    "model": "file-model",
                    "apiKey": "real-secret",
                    "enabled": False,
                },
            }
        ),
        encoding="utf-8",
    )
    config = load_reflex_config(tmp_path / "providers.json", env={})
    assert config.url == "http://127.0.0.1:9999/v1"
    assert config.model == "file-model"
    assert config.api_key == "real-secret"
    assert config.enabled is False


def test_env_disables_even_when_file_enables(tmp_path):
    (tmp_path / "providers.json").write_text(
        json.dumps({"reflex": {"enabled": True}}), encoding="utf-8"
    )
    config = load_reflex_config(
        tmp_path / "providers.json", env={"PET_REFLEX_ENABLED": "0"}
    )
    assert config.enabled is False


def test_providers_json_disable_via_bool_false(tmp_path):
    (tmp_path / "providers.json").write_text(
        json.dumps({"reflex": {"enabled": False}}), encoding="utf-8"
    )
    assert load_reflex_config(tmp_path / "providers.json").enabled is False


def test_providers_json_disable_via_string_zero(tmp_path):
    (tmp_path / "providers.json").write_text(
        json.dumps({"reflex": {"enabled": "0"}}), encoding="utf-8"
    )
    assert load_reflex_config(tmp_path / "providers.json").enabled is False


def test_malformed_providers_json_never_raises(tmp_path):
    (tmp_path / "providers.json").write_text("{not json", encoding="utf-8")
    config = load_reflex_config(tmp_path / "providers.json")
    assert config.url == DEFAULT_REFLEX_URL
    assert config.model == "lfm3-1.7b"
    assert config.enabled is True


def test_unreadable_providers_json_never_raises(tmp_path):
    # Missing parent directory → OSError on read → defaults.
    config = load_reflex_config(tmp_path / "missing-dir" / "providers.json")
    assert config.url == DEFAULT_REFLEX_URL
    assert config.enabled is True


def test_providers_json_without_reflex_section_uses_defaults(tmp_path):
    (tmp_path / "providers.json").write_text(
        json.dumps({"providers": {}}), encoding="utf-8"
    )
    config = load_reflex_config(tmp_path / "providers.json")
    assert config.url == DEFAULT_REFLEX_URL
    assert config.model == "lfm3-1.7b"


# ── Happy path + request contract ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_continuation_parses_and_clamps():
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode("utf-8")))
        # The model continues the "{" prefill with the rest of the object.
        return _content_response('"action":"quiet","think":false,"next_check_s":300,"reason":"深夜安静"}')

    decision = await judge(_NIGHT_STATE, _config(), transport=httpx.MockTransport(handler))

    assert decision["source"] == "reflex"
    assert decision["action"] == "quiet"
    assert decision["think"] is False
    assert decision["next_check_s"] == 300
    assert 240 <= decision["next_check_s"] <= 360
    assert decision["reason"] == "深夜安静"
    assert decision["model"] == "lfm3-1.7b"
    assert isinstance(decision["latency_ms"], int)
    assert "failure" not in decision
    assert len(bodies) == 1


@pytest.mark.asyncio
async def test_prefill_contract_request_shape():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return _content_response('"action":"quiet","think":false,"next_check_s":300,"reason":"深夜安静"}')

    decision = await judge(_NIGHT_STATE, _config(), transport=httpx.MockTransport(handler))
    assert decision["source"] == "reflex"

    body = seen["body"]
    assert seen["url"] == "http://127.0.0.1:1234/v1/chat/completions"
    assert body["model"] == "lfm3-1.7b"
    assert body["max_tokens"] == 128
    assert body["temperature"] == 1.0
    # THE production lever: last message is the assistant prefill "{".
    assert body["messages"][-1] == {"role": "assistant", "content": "{"}
    assert body["messages"][0]["role"] == "system"
    assert body["messages"][1]["role"] == "user"
    # Placeholder key → no Authorization header (laya_router header idiom).
    assert "authorization" not in seen["headers"]
    # strict json_schema present, with the action enum.
    schema = body["response_format"]["json_schema"]
    assert body["response_format"]["type"] == "json_schema"
    assert schema["name"] == "reflex"
    assert schema["strict"] is True
    assert schema["schema"]["required"] == ["action", "think", "next_check_s", "reason"]
    assert schema["schema"]["additionalProperties"] is False
    assert schema["schema"]["properties"]["action"]["enum"] == [
        "speak", "look", "walk", "quiet", "other",
    ]


@pytest.mark.asyncio
async def test_real_api_key_sends_bearer_header():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return _content_response('"action":"quiet","think":false,"next_check_s":300,"reason":"ok"}')

    await judge(
        _NIGHT_STATE,
        _config(api_key="real-secret"),
        transport=httpx.MockTransport(handler),
    )
    assert seen["auth"] == "Bearer real-secret"


# ── Clamping + coercion ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tempo,value,expected",
    [
        ("slow", 1200, 360),
        ("ultra", 1200, 70),
        ("ultra", 5, 50),
        ("fast", 300, 150),
        ("slow", "275.9", 275),  # numeric strings are coerced then clamped
    ],
)
async def test_next_check_s_clamped_to_tier(tempo, value, expected):
    state = dict(_NIGHT_STATE, tempo_mode=tempo)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        return _content_response(
            f'"action":"quiet","think":false,"next_check_s":{value},"reason":"r"}}'
        )

    decision = await judge(state, _config(), transport=httpx.MockTransport(handler))
    assert decision["source"] == "reflex"
    assert decision["next_check_s"] == expected


@pytest.mark.asyncio
async def test_think_string_true_is_coerced():
    def handler(request: httpx.Request) -> httpx.Response:
        return _content_response(
            '"action":"speak","think":"true","next_check_s":100,"reason":"用户在"}'
        )

    decision = await judge(_NIGHT_STATE, _config(), transport=httpx.MockTransport(handler))
    assert decision["source"] == "reflex"
    assert decision["think"] is True
    assert decision["action"] == "speak"


@pytest.mark.asyncio
async def test_long_reason_is_trimmed_to_cap():
    def handler(request: httpx.Request) -> httpx.Response:
        return _content_response(
            '"action":"quiet","think":false,"next_check_s":300,"reason":"' + "很" * 60 + '"}'
        )

    decision = await judge(_NIGHT_STATE, _config(), transport=httpx.MockTransport(handler))
    assert decision["source"] == "reflex"
    assert len(decision["reason"]) <= 40


@pytest.mark.asyncio
async def test_empty_content_and_fence_tolerated():
    async def check(content, action="quiet"):
        def handler(request: httpx.Request) -> httpx.Response:
            return _content_response(content)

        decision = await judge(
            _NIGHT_STATE, _config(), transport=httpx.MockTransport(handler)
        )
        assert decision["source"] == "reflex", (content, decision)
        assert decision["action"] == action

    # Model echoes the "{" prefill before continuing.
    await check('{"action":"quiet","think":false,"next_check_s":300,"reason":"r"}')
    # Code-fenced continuation (defensive; not seen in measurements).
    await check('```\n"action":"look","think":false,"next_check_s":280,"reason":"r"}\n```', "look")


# ── Fallback paths ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content,reason_part",
    [
        ("", "invalid_json"),                                   # empty content ⇒ just "{"
        ("\"action\":\"dance\",\"think\":false,\"next_check_s\":300,\"reason\":\"r\"}", "invalid_action"),
        ("\"action\":\"quiet\",\"think\":\"yes\",\"next_check_s\":300,\"reason\":\"r\"}", "invalid_think"),
        ("\"action\":\"quiet\",\"think\":false,\"next_check_s\":\"soon\",\"reason\":\"r\"}", "invalid_next_check_s"),
        ("not json at all", "invalid_json"),
        # Parses as JSON but next_check_s is missing → field-level failure.
        ("\"action\":\"quiet\",\"think\":false}", "invalid_next_check_s"),
    ],
)
async def test_bad_content_falls_back(content, reason_part):
    def handler(request: httpx.Request) -> httpx.Response:
        return _content_response(content)

    decision = await judge(_NIGHT_STATE, _config(), transport=httpx.MockTransport(handler))
    assert decision["source"] == "fallback"
    assert decision["action"] == "quiet"
    assert decision["think"] is False
    assert decision["next_check_s"] == 300  # slow midpoint
    assert decision["reason"] == ""
    assert reason_part in decision["failure"]


@pytest.mark.asyncio
async def test_http_error_falls_back():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "model busy"})

    decision = await judge(_NIGHT_STATE, _config(), transport=httpx.MockTransport(handler))
    assert decision["source"] == "fallback"
    assert decision["action"] == "quiet"
    assert "http_503" in decision["failure"]


@pytest.mark.asyncio
async def test_timeout_falls_back():
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.3)
        return _content_response('"action":"quiet","think":false,"next_check_s":300,"reason":"r"}')

    decision = await judge(
        _NIGHT_STATE,
        _config(timeout_s=0.05),
        transport=httpx.MockTransport(handler),
    )
    assert decision["source"] == "fallback"
    assert decision["action"] == "quiet"
    assert decision["next_check_s"] == 300
    assert decision["failure"] == "timeout"


@pytest.mark.asyncio
async def test_connect_error_falls_back():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("lm studio down", request=request)

    decision = await judge(_NIGHT_STATE, _config(), transport=httpx.MockTransport(handler))
    assert decision["source"] == "fallback"
    assert "error:ConnectError" in decision["failure"]


@pytest.mark.asyncio
async def test_malformed_payload_shapes_fall_back():
    async def check(payload):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        decision = await judge(
            _NIGHT_STATE, _config(), transport=httpx.MockTransport(handler)
        )
        assert decision["source"] == "fallback"
        assert decision["action"] == "quiet"
        assert decision["next_check_s"] == 300

    await check({})
    await check({"choices": []})
    await check({"choices": [{"message": {"content": 42}}]})
    await check({"choices": "nope"})


@pytest.mark.asyncio
async def test_disabled_config_never_sends_request():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _content_response('"action":"speak","think":true,"next_check_s":60,"reason":"r"}')

    decision = await judge(
        _NIGHT_STATE,
        _config(enabled=False),
        transport=httpx.MockTransport(handler),
    )
    assert calls == 0
    assert decision == {
        "action": "quiet",
        "think": False,
        "next_check_s": 300,
        "reason": "",
        "source": "fallback",
        "failure": "reflex_disabled",
    }


@pytest.mark.asyncio
async def test_fallback_tier_midpoints():
    async def midpoint_for(tier):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down", request=request)

        state = dict(_NIGHT_STATE, tempo_mode=tier)
        return await judge(state, _config(), transport=httpx.MockTransport(handler))

    assert (await midpoint_for("slow"))["next_check_s"] == 300
    assert (await midpoint_for("fast"))["next_check_s"] == 120
    assert (await midpoint_for("ultra"))["next_check_s"] == 60
    # Unknown tempo_mode degrades to the slow tier, never raises.
    assert (await midpoint_for("bogus"))["next_check_s"] == 300


@pytest.mark.asyncio
async def test_state_coerces_and_degrades_without_raising():
    def handler(request: httpx.Request) -> httpx.Response:
        return _content_response('"action":"quiet","think":false,"next_check_s":300,"reason":"r"}')

    for bad_state in ({}, {"tempo_mode": "ULTRA"}, {"new_events": "three"}, None):
        decision = await judge(bad_state, _config(), transport=httpx.MockTransport(handler))
        assert decision["source"] == "reflex"
        assert decision["action"] == "quiet"


# ── Caller-owned client idiom (laya parity) ──────────────────────────────────


@pytest.mark.asyncio
async def test_custom_client_is_not_closed_by_judge():
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: _content_response(
                '"action":"quiet","think":false,"next_check_s":300,"reason":"r"}'
            )
        ),
    )
    try:
        decision = await judge(_NIGHT_STATE, _config(), client=client)
        assert decision["source"] == "reflex"
        assert not client.is_closed
    finally:
        await client.aclose()


# ── State prompt ─────────────────────────────────────────────────────────────


def test_user_prompt_is_compact_chinese_lines_not_json():
    prompt = build_user_prompt(_NIGHT_STATE, "slow")
    assert "状态：深夜 02:00" in prompt
    assert "节奏：slow" in prompt
    assert "240–360" in prompt
    assert "用户新消息：0 条" in prompt
    assert "用户在场：否" in prompt
    assert "空闲：1800 秒" in prompt
    assert "心情：平静" in prompt
    assert "最近心声：好安静；有点困" in prompt
    assert "{" not in prompt and "}" not in prompt
    assert json.dumps(_NIGHT_STATE, ensure_ascii=False) not in prompt
    lines = [line for line in prompt.split("\n") if line]
    assert len(lines) == 7


def test_user_prompt_defaults_fill_missing_state():
    prompt = build_user_prompt({"tempo_mode": "ultra"}, "ultra")
    assert "状态：（暂无摘要）" in prompt
    assert "50–70 秒" in prompt
    assert "用户新消息：0 条" in prompt
    assert "用户在场：否" in prompt
    assert "心情：平静" in prompt
    assert "最近心声：（无）" in prompt


def test_user_prompt_caps_recent_thoughts_at_three():
    prompt = build_user_prompt(
        {"recent_thoughts": ["一", "二", "三", "四", "五"]}, "slow"
    )
    assert "一；二；三" in prompt
    assert "四" not in prompt
