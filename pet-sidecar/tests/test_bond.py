"""bond tests — 羁绊（用户是否在乎我）的独立状态。

覆盖：
- 持久化（load/save 往返、损坏文件不炸、换主题隔离）；
- 等级归一（未知等级 → neutral，标签映射）；
- 小模型 judge_bond：prefill + strict JSON 配方、三形状容错、失败回落；
- 理由截断与"基于实际话语"的 prompt 结构。
"""

from __future__ import annotations

import json

import httpx
import pytest

from gateway.decision import reflex
from gateway.decision.reflex import ReflexConfig
from gateway.memory.bond import (
    BondStore,
    LEVELS,
    clamp_level,
    level_label,
)


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


# ── 状态持久化 ────────────────────────────────────────────────────────


def test_store_defaults_and_roundtrip(tmp_path):
    store = BondStore()
    store.load_from_disk(tmp_path)
    assert store.level == "neutral"
    assert store.label == "一般"
    store.update("warm", "他夸我可爱")
    assert store.level == "warm"
    assert store.note == "他夸我可爱"
    # 重启后还在
    fresh = BondStore()
    fresh.load_from_disk(tmp_path)
    assert fresh.level == "warm"
    assert fresh.note == "他夸我可爱"


def test_store_corrupt_file_does_not_raise(tmp_path):
    (tmp_path / "bond.json").write_text("not json", encoding="utf-8")
    store = BondStore()
    store.load_from_disk(tmp_path)  # must not raise
    assert store.level == "neutral"


def test_store_theme_isolation(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    sa = BondStore(); sa.load_from_disk(a); sa.update("devoted", "很在乎")
    sb = BondStore(); sb.load_from_disk(b)
    assert sb.level == "neutral"  # 新灵魂不继承旧身体的被宠爱


def test_level_clamp_and_labels():
    assert clamp_level("warm") == "warm"
    assert clamp_level("bogus") == "neutral"
    assert clamp_level(None) == "neutral"
    assert level_label("devoted") == "很在乎"
    assert level_label("hostile") == "嫌弃/攻击"
    assert set(LEVELS) == {"devoted", "warm", "neutral", "cold", "hostile"}


def test_summary_line():
    store = BondStore()
    store.load_from_disk(__import__("pathlib").Path(".tmp-bond-test"))
    store.update("cold", "他有点不耐烦")
    assert "有点冷淡" in store.summary_line()
    assert "不耐烦" in store.summary_line()


# ── 小模型判定 ────────────────────────────────────────────────────────


def test_bond_prompt_carries_actual_user_words():
    prompt = reflex.build_bond_prompt("你今天真乖", "嘿嘿")
    assert "你今天真乖" in prompt
    assert "嘿嘿" in prompt
    assert "实际说的话" in prompt


@pytest.mark.asyncio
async def test_judge_bond_short_decision_recipe():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant",
                                     "content": 'warm", "note": "他夸我了"}'}}]
        })

    out = await reflex.judge_bond(
        "你今天真可爱", "嘿嘿", _config(),
        transport=httpx.MockTransport(handler),
    )
    assert out["level"] == "warm"
    assert out["note"] == "他夸我了"
    assert out["source"] == "reflex"
    body = seen["body"]
    assert body["messages"][-1]["role"] == "assistant"
    assert body["messages"][-1]["content"] == reflex._BOND_PREFILL
    assert body["response_format"]["type"] == "json_schema"


@pytest.mark.asyncio
async def test_judge_bond_tolerates_full_object_and_invalid():
    def full(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant",
                                     "content": '{"level": "cold", "note": "他敷衍我"}'}}]
        })
    out = await reflex.judge_bond("哦", "嗯", _config(), transport=httpx.MockTransport(full))
    assert out["level"] == "cold"

    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant", "content": "not json"}}]
        })
    out2 = await reflex.judge_bond("哦", "嗯", _config(), transport=httpx.MockTransport(bad))
    assert out2["source"] == "fallback"
    assert out2["level"] == "neutral"


@pytest.mark.asyncio
async def test_judge_bond_unknown_level_and_disabled():
    def unknown(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"role": "assistant",
                                     "content": 'weird", "note": "x"}'}}]
        })
    out = await reflex.judge_bond("哦", "嗯", _config(), transport=httpx.MockTransport(unknown))
    assert out["level"] == "neutral"

    disabled = await reflex.judge_bond("哦", "嗯", _config(enabled=False))
    assert disabled["source"] == "fallback"


@pytest.mark.asyncio
async def test_judge_bond_never_raises_on_error():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)
    out = await reflex.judge_bond("哦", "嗯", _config(), transport=httpx.MockTransport(boom))
    assert out["source"] == "fallback"
    assert out["level"] == "neutral"
