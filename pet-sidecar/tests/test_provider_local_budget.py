"""Local-endpoint behaviour: loopback detection, output budget, timeout.

LM Studio / llama.cpp / Ollama sit behind the same OpenAI-compatible wire
format as a cloud API, but their economics are different: the user pays for
prefill in wall-clock time. These tests pin the two regressions that made a
2B local model look like it "suddenly stopped":

* the gateway bumped every non-``local`` provider to 8192 output tokens,
  which made LM Studio allocate ``prompt + 8192`` of context;
* nothing distinguished ``http://127.0.0.1:1234/v1`` from a cloud endpoint,
  so the HTTP read timeout stayed at the cloud-sized 60 s.
"""

from __future__ import annotations

import json

import httpx
import pytest

from gateway.providers.openai import LMSTUDIO_LOW_THINKING_BUDGET, OpenAIProvider, is_local_url
from gateway.server import ChatRequest, _output_budget


class _FakeProvider:
    def __init__(self, name: str, is_local: bool) -> None:
        self.name = name
        self.is_local = is_local


def _req(max_new_tokens: int = 768, thinking: bool = False) -> ChatRequest:
    return ChatRequest(
        messages=[{"role": "user", "content": "你好"}],
        max_new_tokens=max_new_tokens,
        thinking=thinking,
    )


def test_is_local_url_recognises_loopback_forms():
    assert is_local_url("http://127.0.0.1:1234/v1")
    assert is_local_url("http://localhost:11434/v1")
    assert is_local_url("http://[::1]:8080/v1")
    assert is_local_url("http://0.0.0.0:8000/v1")


def test_is_local_url_rejects_remote_endpoints():
    assert not is_local_url("https://api.deepseek.com/v1")
    assert not is_local_url("https://api.siliconflow.cn/v1")
    assert not is_local_url("")


def test_provider_detects_local_base_url():
    local = OpenAIProvider(base_url="http://127.0.0.1:1234/v1", model="minicpm5-2b")
    cloud = OpenAIProvider(base_url="https://api.deepseek.com/v1", model="deepseek")
    assert local.is_local is True
    assert cloud.is_local is False


def test_provider_allows_explicit_is_local_override():
    # e.g. LM Studio exposed through a LAN address / SSH tunnel — still the
    # user's own GPU, still needs the local timeout and output budget.
    p = OpenAIProvider(base_url="http://192.168.1.20:1234/v1", model="x", is_local=True)
    assert p.is_local is True


def test_local_output_budget_keeps_renderer_value():
    provider = _FakeProvider("lmstudio", is_local=True)
    assert _output_budget(provider, _req(768)) == 768
    assert _output_budget(provider, _req(1280, thinking=True)) == 1280


def test_cloud_output_budget_keeps_8192_floor():
    provider = _FakeProvider("deepseek", is_local=False)
    assert _output_budget(provider, _req(768)) == 8192


def test_builtin_local_provider_keeps_renderer_value():
    provider = _FakeProvider("local", is_local=False)
    assert _output_budget(provider, _req(768)) == 768


def test_provider_tools_default_on_for_direct_construction():
    # Direct construction keeps the old default; the server registration
    # turns tools off for loopback base URLs (providers.json can opt back
    # in with "toolsEnabled": true).
    p = OpenAIProvider(base_url="http://127.0.0.1:1234/v1", model="minicpm5-2b")
    assert p.allow_tools is True


def test_provider_allow_tools_can_be_disabled():
    p = OpenAIProvider(
        base_url="http://127.0.0.1:1234/v1", model="minicpm5-2b", allow_tools=False
    )
    assert p.allow_tools is False


def test_provider_function_calling_can_be_disabled():
    # A browser-driven web-chat bridge speaks the OpenAI wire format but has
    # no native tool calls; the renderer must keep the text-MCP prompt.
    p = OpenAIProvider(
        base_url="http://127.0.0.1:8766/v1",
        model="deepseek-web",
        supports_function_calling=False,
    )
    assert p.supports_function_calling is False
    assert OpenAIProvider(base_url="https://api.deepseek.com/v1").supports_function_calling is True


class _CompletionRecorder:
    def __init__(self, first_error: str | None = None) -> None:
        self.bodies: list[dict] = []
        self.first_error = first_error

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        self.bodies.append(json.loads(request.content))
        if self.first_error and len(self.bodies) == 1:
            return httpx.Response(400, json={"error": {"message": self.first_error}})
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            b'data: [DONE]\n\n',
        )


def _mock_client(provider: OpenAIProvider, recorder: _CompletionRecorder) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=provider._base_url,
        transport=httpx.MockTransport(recorder),
        trust_env=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("thinking", [True, False, None])
async def test_lmstudio_thinking_budget_tristate(thinking):
    provider = OpenAIProvider(
        name="lmstudio",
        base_url="http://127.0.0.1:1234/v1",
        model="ornith",
        thinking=thinking,
        reasoning_effort="max",
    )
    recorder = _CompletionRecorder()
    async with _mock_client(provider, recorder) as client:
        provider._client = client
        events = [
            event async for event in provider.chat(
                [{"role": "user", "content": "hi"}], reasoning_effort_override="high"
            )
        ]

    assert events == [{"type": "delta", "content": "ok"}, {"type": "end"}]
    assert len(recorder.bodies) == 1
    body = recorder.bodies[0]
    if thinking is None:
        assert "thinking_budget_tokens" not in body
    elif thinking:
        assert "thinking_budget_tokens" in body
        assert body["thinking_budget_tokens"] is None
    else:
        assert body["thinking_budget_tokens"] == 0
    assert "thinking" not in body
    assert "reasoning_effort" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("override,effort,configured,expected", [
    (True, "low", None, LMSTUDIO_LOW_THINKING_BUDGET),
    (True, "high", None, None),
    (True, None, "low", LMSTUDIO_LOW_THINKING_BUDGET),
    (True, None, None, None),
    (False, "low", None, 0),
])
async def test_lmstudio_low_effort_bounds_reasoning_budget(override, effort, configured, expected):
    provider = OpenAIProvider(
        name="lmstudio",
        base_url="http://127.0.0.1:1234/v1",
        model="ornith",
        thinking=True,
        reasoning_effort=configured,
    )
    recorder = _CompletionRecorder()
    async with _mock_client(provider, recorder) as client:
        provider._client = client
        events = [
            event async for event in provider.chat(
                [{"role": "user", "content": "hi"}],
                thinking_override=override,
                reasoning_effort_override=effort,
            )
        ]
    assert events == [{"type": "delta", "content": "ok"}, {"type": "end"}]
    assert recorder.bodies[0]["thinking_budget_tokens"] == expected
    assert "reasoning_effort" not in recorder.bodies[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_thinking", [True, False, None])
async def test_lmstudio_round_override_disables_then_clears_budget(configured_thinking):
    provider = OpenAIProvider(
        name="lmstudio",
        base_url="http://127.0.0.1:1234/v1",
        model="ornith",
        thinking=configured_thinking,
        reasoning_effort="max",
    )
    recorder = _CompletionRecorder()
    async with _mock_client(provider, recorder) as client:
        provider._client = client
        for override in (False, True, None):
            events = [
                event async for event in provider.chat(
                    [{"role": "user", "content": "hi"}],
                    thinking_override=override,
                    reasoning_effort_override="high",
                )
            ]
            assert events == [{"type": "delta", "content": "ok"}, {"type": "end"}]

    assert len(recorder.bodies) == 3
    assert recorder.bodies[0]["thinking_budget_tokens"] == 0
    assert "thinking_budget_tokens" in recorder.bodies[1]
    assert recorder.bodies[1]["thinking_budget_tokens"] is None
    default_body = recorder.bodies[2]
    if configured_thinking is None:
        assert "thinking_budget_tokens" not in default_body
    elif configured_thinking:
        assert "thinking_budget_tokens" in default_body
        assert default_body["thinking_budget_tokens"] is None
    else:
        assert default_body["thinking_budget_tokens"] == 0
    assert provider._thinking is configured_thinking
    assert all("thinking" not in body for body in recorder.bodies)
    assert all("reasoning_effort" not in body for body in recorder.bodies)


@pytest.mark.asyncio
@pytest.mark.parametrize("thinking", [True, False, None])
@pytest.mark.parametrize(
    "name,base_url,is_local",
    [
        ("openai", "http://127.0.0.1:1234/v1", None),
        ("llama.cpp", "http://localhost:8080/v1", None),
        ("local", "http://[::1]:8080/v1", None),
        ("ollama", "http://localhost:11434/v1", None),
        ("openai", "http://192.168.1.20:1234/v1", True),
        ("openai", "https://api.openai.com/v1", None),
        ("deepseek", "https://api.deepseek.com/v1", None),
        ("deepseek-web", "http://127.0.0.1:8766/v1", None),
        ("openai", "http://127.0.0.1:8766/v1", None),
    ],
)
async def test_other_providers_never_send_lmstudio_budget(name, base_url, is_local, thinking):
    provider = OpenAIProvider(
        name=name,
        base_url=base_url,
        model="ornith",
        is_local=is_local,
        thinking=True,
        reasoning_effort="max",
    )
    recorder = _CompletionRecorder()
    async with _mock_client(provider, recorder) as client:
        provider._client = client
        events = [
            event async for event in provider.chat(
                [{"role": "user", "content": "hi"}], thinking_override=thinking
            )
        ]

    assert events == [{"type": "delta", "content": "ok"}, {"type": "end"}]
    assert len(recorder.bodies) == 1
    body = recorder.bodies[0]
    assert "thinking_budget_tokens" not in body
    if thinking is False:
        assert "reasoning_effort" not in body
        if provider.is_local:
            assert "thinking" not in body
        else:
            assert body["thinking"] == {"type": "disabled"}
    else:
        assert body["thinking"] == {"type": "enabled"}
        assert body["reasoning_effort"] == "max"


@pytest.mark.asyncio
@pytest.mark.parametrize("thinking", [True, False])
@pytest.mark.parametrize(
    "message",
    [
        "Unsupported parameter: thinking_budget_tokens",
        "Unrecognized request argument supplied: thinking_budget_tokens",
    ],
)
async def test_lmstudio_rejected_budget_is_diagnostic_without_retry(thinking, message):
    provider = OpenAIProvider(
        name="lmstudio",
        base_url="http://127.0.0.1:1234/v1",
        model="ornith",
        thinking=thinking,
    )
    # A retry would return success, concealing an unsupported off switch.
    recorder = _CompletionRecorder(first_error=message)
    async with _mock_client(provider, recorder) as client:
        provider._client = client
        events = [event async for event in provider.chat([{"role": "user", "content": "hi"}])]
        assert len(recorder.bodies) == 1
        assert len(events) == 1
        assert events[0]["type"] == "error"
        assert "不支持当前推理开关" in events[0]["message"]
        assert "HTTP 400" in events[0]["message"]
        assert message in events[0]["message"]
        assert recorder.bodies[0]["thinking_budget_tokens"] == (None if thinking else 0)
        assert provider._thinking_unsupported is False

        # Rejection must not cache a downgrade that drops the next round's budget.
        next_events = [
            event async for event in provider.chat([{"role": "user", "content": "hi"}])
        ]
    assert len(recorder.bodies) == 2
    assert recorder.bodies[1]["thinking_budget_tokens"] == (None if thinking else 0)
    assert next_events == [{"type": "delta", "content": "ok"}, {"type": "end"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Unsupported parameter: thinking",
        "The reasoning_content in the thinking mode must be passed back to the API.",
    ],
)
async def test_cloud_thinking_400_keeps_existing_retry(message):
    provider = OpenAIProvider(
        name="deepseek",
        base_url="https://api.deepseek.com/v1",
        thinking=True,
        reasoning_effort="max",
    )
    recorder = _CompletionRecorder(first_error=message)
    async with _mock_client(provider, recorder) as client:
        provider._client = client
        events = [event async for event in provider.chat([{"role": "user", "content": "hi"}])]

    assert events == [{"type": "delta", "content": "ok"}, {"type": "end"}]
    assert len(recorder.bodies) == 2
    assert recorder.bodies[0]["thinking"] == {"type": "enabled"}
    assert recorder.bodies[0]["reasoning_effort"] == "max"
    assert "thinking" not in recorder.bodies[1]
    assert "reasoning_effort" not in recorder.bodies[1]
    assert all("thinking_budget_tokens" not in body for body in recorder.bodies)
    assert provider._thinking_unsupported is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_url",
    ["http://127.0.0.1:1234/v1", "http://[::1]:1234/v1", "https://api.openai.com/v1"],
)
async def test_provider_client_ignores_shell_proxy_environment(monkeypatch, base_url):
    monkeypatch.setenv("NO_PROXY", "localhost,[::1],127.0.0.1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    provider = OpenAIProvider(base_url=base_url)
    try:
        client = provider._get_client()
        assert client.trust_env is False
    finally:
        await provider.close()
