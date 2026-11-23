"""reflex.py - 快直觉核心：一次便宜的小模型状态判断（System 0）。

The desk pet's main brain (the big model) is expensive, so every tempo
tick is first judged here by lfm3-1.7b on LM Studio. The reflex decides
whether the tick can be spent by the body alone (quiet/look/walk/...) or
whether the main brain should wake up (think=true). Quiet rounds never
wake the brain — that IS the token saving.

Why the odd request shape (measured on lfm3-1.7b, 2026-11, trust these):
    * reasoning-first model: naive calls burn every max_token on thinking
      and return EMPTY content (0/30 valid JSON). ``chat_template_kwargs:
      {"enable_thinking": false}`` is ignored and there is no
      assistant_reasoning role;
    * THE production lever (100/100 strict + field-valid over 70 calls):
      END the messages array with a partial assistant turn (prefill "{")
      AND set ``response_format: json_schema``. The model then continues
      the prefill straight into the JSON body (reasoning_tokens=0, so
      max_tokens=128 is plenty) and the full object is ``prefill + content``;
    * json_schema only constrains the content phase — the prefill is what
      defeats the thinking phase.

Calibration bias guard: static deep-night states dominated early trials
(think=true, next_check_s at the range floor, look-bias). The system
prompt therefore explicitly steers quiet nights to action=quiet /
think=false / next_check_s near the tempo ceiling, and demands a real
cause (new events / user present / strong intuition) for any upgrade.

Nothing connects at import time. ``judge`` opens a short-lived
``httpx.AsyncClient`` per call — the exact laya_router idiom (there is no
module-level client singleton or reset idiom in laya_router to copy) —
unless the caller injects a client or a transport. Every network wait is
bounded and asynchronous. No decision caching across calls: each tick is
state-dependent. Every failure path returns a fallback decision; judge
NEVER raises — the pet's reflex must be unbreakable.

Environment:
    PET_REFLEX_URL      base URL (default http://127.0.0.1:1234/v1)
    PET_REFLEX_MODEL    model id (default lfm3-1.7b)
    PET_REFLEX_ENABLED  "0"/"false"/"no"/"off" disables (default on)

Config file (~/.pet/providers.json, top-level "reflex" section):
    {"reflex": {"baseUrl": ..., "model": ..., "apiKey": ..., "enabled": ...}}

Resolution order (first wins): env > providers.json "reflex" > defaults.
A missing or unreadable providers.json means all defaults, never raises.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

DEFAULT_REFLEX_URL = "http://127.0.0.1:1234/v1"
DEFAULT_REFLEX_MODEL = "lfm3-1.7b"
# LM Studio's conventional placeholder. A Bearer header is only sent for a
# real secret (see _auth_headers); a trusted loopback service gets no
# pointless headers, mirroring laya_router's header behaviour.
DEFAULT_REFLEX_API_KEY = "lm-studio"
# The partial assistant turn that defeats lfm3's thinking phase.
DEFAULT_REFLEX_PREFILL = "{"
# Measured: with the prefill defeating the thinking phase reasoning_tokens=0,
# so 128 completion tokens covers the whole JSON object with headroom.
DEFAULT_REFLEX_MAX_TOKENS = 128
# Measured p50 ~2.05 s, ~2.72 s under load; 4 s keeps the reflex off the
# life-tick critical path while leaving tail room before the fallback.
DEFAULT_REFLEX_TIMEOUT_S = 4.0

REFLEX_URL_ENV = "PET_REFLEX_URL"
REFLEX_MODEL_ENV = "PET_REFLEX_MODEL"
REFLEX_ENABLED_ENV = "PET_REFLEX_ENABLED"

REFLEX_ENDPOINT = "/chat/completions"

ACTIONS: Tuple[str, ...] = ("speak", "look", "walk", "quiet", "other")

# Interval bounds (seconds) per tempo tier. These MUST mirror server.py
# _TEMPO_SPECS["interval_s"] — a wiring-time parity test exists elsewhere;
# change both files together or the reflex clamps to the wrong cadence.
TEMPO_RANGE_S: Dict[str, Tuple[int, int]] = {
    "slow": (240, 360),
    "fast": (90, 150),
    "ultra": (50, 70),
}
TEMPO_DEFAULT = "slow"

# The prompt asks for ≤20 中文字; validation is a looser hard cap so a
# slightly chatty model answer is trimmed, not wasted.
_REASON_MAX_CHARS = 40
_FAILURE_MAX_CHARS = 80
_THOUGHT_MAX_CHARS = 40
_MAX_RECENT_THOUGHTS = 3

_DISABLED_WORDS = {"0", "false", "no", "off", "disabled"}

_API_KEY_PLACEHOLDERS = {
    "lm-studio",
    "lmstudio",
    "lm_studio",
    "none",
    "null",
    "changeme",
    "placeholder",
    "xxx",
    "sk-xxx",
}

# strict json_schema: constrains the content phase only (the prefill handles
# the thinking phase). Required fields exactly match the output contract.
_REFLEX_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "think": {"type": "boolean"},
        "next_check_s": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["action", "think", "next_check_s", "reason"],
    "additionalProperties": False,
}
_RESPONSE_FORMAT: Dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {"name": "reflex", "strict": True, "schema": _REFLEX_SCHEMA},
}

# 快直觉 system prompt. Calibration text is deliberate: the measured bias
# was static deep-night states → think=true / next_check_s at the range
# floor / look-bias. Do not "simplify" these lines away.
_SYSTEM_PROMPT = """你是桌宠的快直觉核心（潜意识）。职责：用一次便宜的状态判断决定这一拍怎么过——值得的事才唤醒大脑（主模型），安静的时候就让身体安静待着，不要为了存在感而行动。

输出契约：只输出一个 JSON 对象，字段固定为：
{"action": "speak"|"look"|"walk"|"quiet"|"other", "think": false, "next_check_s": 300, "reason": "不超过20字的中文理由"}
- action：这一拍身体做什么。speak=说一句话，look=看一眼周围，walk=走动，quiet=安静待着，other=其它小动作。
- think：是否唤醒大脑做真正的思考。没有明确理由就是 false。
- next_check_s：距下次自查的秒数，必须落在当前节奏档内。
- reason：最关键的一句话，不超过20个中文字；升级（非 quiet/非 think=false）时必须写出理由。

节奏锚定（next_check_s 必须落在当前档位区间内，用户消息会给出当前档位）：
- slow：240–360 秒；fast：90–150 秒；ultra：50–70 秒。

校准指导：
- 深夜安静、没有新事件、用户不在：action=quiet、think=false，next_check_s 靠近档位上限（slow 档取 330–360）。
- 只有出现新事件、用户在场、或强烈的直觉时才升级 action 或 think=true；不要无缘无故地看或走。
- 不要连续多轮输出相同的 action；最近心声显示上一轮已做过某动作时，本轮换一种更合适的方式。
- 拿不准就选更省的一项：quiet、think=false、档位内偏大的 next_check_s。"""


@dataclass(frozen=True, slots=True)
class ReflexConfig:
    """Resolved reflex settings (env > providers.json "reflex" > defaults)."""

    url: str = DEFAULT_REFLEX_URL
    model: str = DEFAULT_REFLEX_MODEL
    api_key: str = DEFAULT_REFLEX_API_KEY
    prefill: str = DEFAULT_REFLEX_PREFILL
    max_tokens: int = DEFAULT_REFLEX_MAX_TOKENS
    timeout_s: float = DEFAULT_REFLEX_TIMEOUT_S
    enabled: bool = True

    def as_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "model": self.model,
            "api_key": self.api_key,
            "prefill": self.prefill,
            "max_tokens": self.max_tokens,
            "timeout_s": self.timeout_s,
            "enabled": self.enabled,
        }


def _default_providers_path() -> Path:
    # Same location server.py loads API providers from.
    return Path.home() / ".pet" / "providers.json"


def _section_str(section: Mapping[str, Any], key: str) -> str:
    value = section.get(key)
    return str(value).strip() if value is not None else ""


def _flag(value: Any, default: bool) -> bool:
    """Tolerant bool for file-side values ("0" → False, missing → default)."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if not text:
        return default
    return text not in _DISABLED_WORDS


def load_reflex_config(
    providers_json_path: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> ReflexConfig:
    """Resolve ReflexConfig: env > providers.json top-level "reflex" > defaults.

    Missing/unreadable/malformed providers.json → all defaults, never raises.
    """
    env_map: Mapping[str, str] = os.environ if env is None else env

    try:
        path = (
            Path(providers_json_path)
            if providers_json_path is not None
            else _default_providers_path()
        )
    except (TypeError, ValueError):
        path = _default_providers_path()

    section: Dict[str, Any] = {}
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("reflex"), dict):
                section = data["reflex"]
    except (OSError, ValueError, UnicodeError):
        section = {}

    env_url = str(env_map.get(REFLEX_URL_ENV) or "").strip()
    url = env_url or _section_str(section, "baseUrl") or DEFAULT_REFLEX_URL
    url = url.rstrip("/") or DEFAULT_REFLEX_URL

    env_model = str(env_map.get(REFLEX_MODEL_ENV) or "").strip()
    model = env_model or _section_str(section, "model") or DEFAULT_REFLEX_MODEL

    env_enabled = str(env_map.get(REFLEX_ENABLED_ENV) or "").strip().lower()
    if env_enabled:
        enabled = env_enabled not in _DISABLED_WORDS
    else:
        enabled = _flag(section.get("enabled"), True)

    api_key = _section_str(section, "apiKey") or DEFAULT_REFLEX_API_KEY

    return ReflexConfig(
        url=url,
        model=model,
        api_key=api_key,
        prefill=DEFAULT_REFLEX_PREFILL,
        max_tokens=DEFAULT_REFLEX_MAX_TOKENS,
        timeout_s=DEFAULT_REFLEX_TIMEOUT_S,
        enabled=enabled,
    )


def _resolve_tier(state: Mapping[str, Any]) -> str:
    tier = str(state.get("tempo_mode") or "").strip().lower()
    return tier if tier in TEMPO_RANGE_S else TEMPO_DEFAULT


def _as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return default


def build_user_prompt(state: Mapping[str, Any], tier: str) -> str:
    """Compact Chinese state lines — never a raw JSON dump of the state."""
    lo, hi = TEMPO_RANGE_S[tier]
    summary = str(state.get("summary") or "").strip() or "（暂无摘要）"
    thoughts = []
    for item in (state.get("recent_thoughts") or []):
        text = str(item).strip()
        if text:
            thoughts.append(text[:_THOUGHT_MAX_CHARS])
        if len(thoughts) >= _MAX_RECENT_THOUGHTS:
            break
    lines = [
        f"状态：{summary}",
        f"节奏：{tier}（next_check_s 取 {lo}–{hi} 秒）",
        f"用户新消息：{_as_int(state.get('new_events'))} 条",
        f"用户在场：{'是' if state.get('user_present') else '否'}",
        f"空闲：{_as_int(state.get('idle_s'))} 秒",
        f"心情：{str(state.get('mood') or '').strip() or '平静'}",
        "最近心声：" + ("；".join(thoughts) if thoughts else "（无）"),
    ]
    return "\n".join(lines)


def _build_payload(state: Mapping[str, Any], tier: str, config: ReflexConfig) -> dict:
    """The exact production request shape for lfm3-1.7b (see module docstring)."""
    return {
        "model": config.model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(state, tier)},
            # THE lever: partial assistant turn continues straight into JSON.
            {"role": "assistant", "content": config.prefill},
        ],
        "response_format": _RESPONSE_FORMAT,
        "max_tokens": config.max_tokens,
        "temperature": 1.0,
    }


def _auth_headers(api_key: str) -> Dict[str, str]:
    """Bearer only for a non-empty, non-placeholder key (laya behaviour)."""
    key = str(api_key or "").strip()
    if not key or key.lower() in _API_KEY_PLACEHOLDERS:
        return {}
    return {"Authorization": f"Bearer {key}"}


_FENCE_START_RE = re.compile(r"^```[A-Za-z0-9_-]*\s*")
_FENCE_END_RE = re.compile(r"\s*```\s*$")


def _continuation_candidate(content: Any, prefill: str) -> str:
    """prefill + returned content; tolerates an echoed "{" or code fences."""
    raw = str(content or "").strip()
    if raw.startswith("```"):
        raw = _FENCE_START_RE.sub("", raw)
        raw = _FENCE_END_RE.sub("", raw).strip()
    if prefill and raw.startswith(prefill):
        raw = raw[len(prefill):]
    return prefill + raw


def _validate_decision(data: Any, tier: str) -> Tuple[Optional[dict], str]:
    """Pure (decision, failure) validation. failure == "" means success."""
    if not isinstance(data, dict):
        return None, "invalid_json"
    lo, hi = TEMPO_RANGE_S[tier]

    action = data.get("action")
    if not isinstance(action, str) or action not in ACTIONS:
        return None, f"invalid_action:{str(action)[:24]}"

    think = data.get("think")
    if isinstance(think, str):
        lowered = think.strip().lower()
        if lowered in ("true", "false"):
            think = lowered == "true"
    if not isinstance(think, bool):
        return None, f"invalid_think:{str(think)[:24]}"

    raw_check = data.get("next_check_s")
    if isinstance(raw_check, bool):
        return None, "invalid_next_check_s"
    try:
        next_check = int(float(raw_check))
    except (TypeError, ValueError, OverflowError):
        return None, "invalid_next_check_s"
    next_check = max(lo, min(hi, next_check))

    raw_reason = data.get("reason")
    reason = str(raw_reason).strip() if raw_reason is not None else ""
    reason = reason[:_REASON_MAX_CHARS]

    return {
        "action": action,
        "think": think,
        "next_check_s": next_check,
        "reason": reason,
        "source": "reflex",
    }, ""


def _fallback(tier: str, failure: str) -> dict:
    """The unbreakable quiet decision: cheap, safe, and inside the tier."""
    lo, hi = TEMPO_RANGE_S[tier]
    return {
        "action": "quiet",
        "think": False,
        "next_check_s": (lo + hi) // 2,
        "reason": "",
        "source": "fallback",
        "failure": failure[:_FAILURE_MAX_CHARS],
    }


async def _post(
    client: httpx.AsyncClient,
    config: ReflexConfig,
    payload: dict,
    tier: str,
) -> "dict | str":
    """One bounded attempt. Returns the decision dict or a failure string."""
    url = config.url.rstrip("/") + REFLEX_ENDPOINT
    try:
        response = await asyncio.wait_for(
            client.post(url, json=payload, headers=_auth_headers(config.api_key)),
            timeout=config.timeout_s,
        )
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return "timeout"
    except Exception as exc:  # connect/proxy/protocol errors
        return f"error:{type(exc).__name__}"

    try:
        if response.status_code != 200:
            return f"http_{response.status_code}"
        try:
            data = response.json()
        except Exception as exc:
            return f"invalid_json:{type(exc).__name__}"

        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            return "invalid_payload"
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None

        candidate = _continuation_candidate(content, config.prefill)
        try:
            parsed = json.loads(candidate)
        except Exception:
            # Includes the measured failure mode: reasoning burned the budget
            # and content came back empty ⇒ candidate is just "{".
            return "invalid_json:continuation"
        decision, failure = _validate_decision(parsed, tier)
        if decision is None:
            return failure
        return decision
    finally:
        try:
            await response.aclose()
        except Exception:
            pass


async def judge(
    state: dict,
    config: ReflexConfig,
    *,
    client: Optional[httpx.AsyncClient] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
    now: Optional[float] = None,
) -> dict:
    """One cheap reflex judgment for one life tick.

    Args:
        state: caller-built snapshot:
            {
              "summary": str,            # DSTATE 摘要（一行）
              "tempo_mode": "slow"|"fast"|"ultra",
              "new_events": int,         # 新用户消息条数
              "user_present": bool,
              "idle_s": float,           # 距上次用户交互的秒数
              "mood": str,               # 粗粒度心情词
              "recent_thoughts": [str],  # 最近心声，≤3 条
            }
            Missing/unknown keys degrade gracefully; unknown tempo_mode → "slow".
        config: ReflexConfig from load_reflex_config().
        client: optional caller-owned AsyncClient (not closed here).
        transport: optional httpx transport (MockTransport in tests) for the
            short-lived client this module creates.
        now: caller's epoch timestamp for this tick; accepted for wiring
            symmetry — the decision itself is time-free (recency arrives via
            state["idle_s"]).

    Returns:
        Success: {"action", "think", "next_check_s", "reason",
                  "source": "reflex", "latency_ms", "model"}.
        ANY failure (disabled / timeout / HTTP error / non-JSON / invalid
        fields): {"action": "quiet", "think": False,
        "next_check_s": <tier midpoint>, "reason": "",
        "source": "fallback", "failure": "<short reason>"}.
        judge NEVER raises.
    """
    if not isinstance(state, dict):
        state = {}
    tier = _resolve_tier(state)

    if not config.enabled:
        return _fallback(tier, "reflex_disabled")

    payload = _build_payload(state, tier, config)
    started = time.perf_counter()
    try:
        if client is not None:
            outcome = await _post(client, config, payload, tier)
        else:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(config.timeout_s),
                transport=transport,
                trust_env=False,  # never let a proxy intercept loopback
            ) as http:
                outcome = await _post(http, config, payload, tier)
    except Exception as exc:
        # Last-resort guard: a reflex bug must never take a life tick down.
        logger.debug("reflex unexpected error", exc_info=True)
        outcome = f"error:{type(exc).__name__}"

    if isinstance(outcome, str):
        logger.debug("reflex fallback (%s)", outcome)
        return _fallback(tier, outcome)

    outcome["latency_ms"] = int((time.perf_counter() - started) * 1000)
    outcome["model"] = config.model
    return outcome


def adjust_config(config: ReflexConfig, **changes: Any) -> ReflexConfig:
    """Typed sugar over dataclasses.replace for wiring-time tweaks."""
    return replace(config, **changes)


__all__ = [
    "ACTIONS",
    "DEFAULT_REFLEX_MAX_TOKENS",
    "DEFAULT_REFLEX_MODEL",
    "DEFAULT_REFLEX_PREFILL",
    "DEFAULT_REFLEX_TIMEOUT_S",
    "DEFAULT_REFLEX_URL",
    "REFLEX_ENABLED_ENV",
    "REFLEX_MODEL_ENV",
    "REFLEX_URL_ENV",
    "TEMPO_DEFAULT",
    "TEMPO_RANGE_S",
    "ReflexConfig",
    "adjust_config",
    "build_user_prompt",
    "judge",
    "load_reflex_config",
]
