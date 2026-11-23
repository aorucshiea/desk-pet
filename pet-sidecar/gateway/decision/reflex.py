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
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

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
# Measured p50 ~2.05 s in a clean benchmark; IN PRODUCTION the same request
# shape (system + state + prefill + strict json_schema, 128 tokens) measured
# 7–20 s while the main 35B model shares the LM Studio box (CPU-only, both
# models resident). The production measurement, not the benchmark, is what
# matters: at 4 s / 8 s EVERY tick fell back to quiet and the small model
# never decided. 25 s still fits the fastest tempo tier (50–70 s), so a slow
# tick costs cadence, never correctness; the fallback stays for a dead endpoint.
DEFAULT_REFLEX_TIMEOUT_S = 25.0

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
        "reaction": {"type": "string", "enum": ["small", "medium", "large"]},
        "next_check_s": {"type": "integer"},
        "reason": {"type": "string"},
        # 固定问题 4–6（时间感）：和 1–3 一样是必答项，不是可选的提示词。
        "time_doing": {"type": "string"},
        "time_relevant": {"type": "boolean"},
        "time_note": {"type": "string"},
    },
    "required": [
        "action", "think", "reaction", "next_check_s", "reason",
        "time_doing", "time_relevant", "time_note",
    ],
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
{"action": "speak"|"look"|"walk"|"quiet"|"other", "think": false, "reaction": "small", "next_check_s": 300, "reason": "不超过20字的中文理由", "time_doing": "刚才我在做什么", "time_relevant": false, "time_note": "过去了多久"}
- action：这一拍身体做什么。speak=说一句话，look=看一眼周围，walk=走动，quiet=安静待着，other=其它小动作。
- think：是否唤醒大脑做真正的思考。没有明确理由就是 false。
- reaction：升级时大脑的持续反应力度。small=简短回应(1536 tok)、medium=中等(2560 tok)、large=深入(4096 tok)。quiet 时选 small。
- next_check_s：距下次自查的秒数，必须落在当前节奏档内。
- reason：最关键的一句话，不超过20个中文字；升级（非 quiet/非 think=false）时必须写出理由。
- time_doing：你之前或者刚刚在做什么？看状态摘要与最近心声，用一行短句回答（不超过20字），不知道就空字符串。
- time_relevant：这件事跟时间有关吗？只有确实需要知道现在的时间（比如睡觉、等待、日常节奏）才为 true；其他情况 false。系统只在你标记 true 后才把时间给你。
- time_note：你觉得“之前/刚刚做的事已经过去了多久”的估计（用最近心声的时间线推）；不知道就留空字符串。

节奏锚定（next_check_s 必须落在当前档位区间内，用户消息会给出当前档位）：
- slow：240–360 秒；fast：90–150 秒；ultra：50–70 秒。

校准指导：
- 深夜安静、没有新事件、用户不在：action=quiet、think=false，next_check_s 靠近档位上限（slow 档取 330–360）。
- 只有出现新事件、用户在场、或强烈的直觉时才升级 action 或 think=true；不要无缘无故地看或走。
- 升级时根据事情的重要程度选 reaction：小事 small、需要认真回复 medium、复杂问题 large。
- 不要连续多轮输出相同的 action；最近心声显示上一轮已做过某动作时，本轮换一种更合适的方式。
- 拿不准就选更省的一项：quiet、think=false、reaction=small、档位内偏大的 next_check_s。"""


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
        "【D 的固定问题】（4–6 在 time_doing / time_relevant / time_note 里作答）",
        "1. 这一拍用多强的思考？→ think",
        "2. 这一拍正文写多长？→ reaction",
        "3. 接下来最自然的默认动作？→ action",
        "4. 之前或者刚刚我在做什么？看状态摘要与最近心声；不知道就空着。",
        "5. 我需要知道现在的时间吗？只有事情跟时间相关才需要——需要时 time_relevant=true。",
        "6. 我之前或者刚刚做的事情已经过去了多久？若重要，用最近心声的时间线估计写进 time_note；不知道就留空。",
        "【这一拍的状态】",
        f"状态：{summary}",
        f"节奏：{tier}（next_check_s 取 {lo}–{hi} 秒）",
        f"用户新消息：{_as_int(state.get('new_events'))} 条",
        f"用户在场：{'是' if state.get('user_present') else '否'}",
        f"空闲：{_as_int(state.get('idle_s'))} 秒",
        f"心情：{str(state.get('mood') or '').strip() or '平静'}",
        "最近心声：" + ("；".join(thoughts) if thoughts else "（无）"),
    ]
    # 时间是"模型求时才给"：只有上一轮它标了 time_relevant，这一轮才附。
    current_time = str(state.get("current_time") or "").strip()
    if current_time:
        lines.append(f"当前时间：{current_time}")
    elapsed = state.get("elapsed_since_main_s")
    if isinstance(elapsed, (int, float)) and elapsed > 0:
        lines.append(f"距上次主脑轮：{int(elapsed // 60)} 分钟")
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

    raw_reaction = data.get("reaction")
    reaction = str(raw_reaction).strip().lower() if raw_reaction else "small"
    if reaction not in ("small", "medium", "large"):
        reaction = "small"

    raw_time_rel = data.get("time_relevant")
    if isinstance(raw_time_rel, str):
        time_relevant = raw_time_rel.strip().lower() == "true"
    else:
        time_relevant = bool(raw_time_rel) if raw_time_rel is not None else False
    time_doing = str(data.get("time_doing") or "").strip()[:_REASON_MAX_CHARS]
    time_note = str(data.get("time_note") or "").strip()[:_REASON_MAX_CHARS]

    return {
        "action": action,
        "think": think,
        "reaction": reaction,
        "next_check_s": next_check,
        "reason": reason,
        "time_doing": time_doing,
        "time_relevant": time_relevant,
        "time_note": time_note,
        "source": "reflex",
    }, ""


def _fallback(tier: str, failure: str) -> dict:
    """The unbreakable quiet decision: cheap, safe, and inside the tier."""
    lo, hi = TEMPO_RANGE_S[tier]
    return {
        "action": "quiet",
        "think": False,
        "reaction": "small",
        "time_doing": "",
        "time_relevant": False,
        "time_note": "",
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


# 思考档快直觉（reflect）：其余小模型工作（潜意识出题、睡眠批答、晨间批阅
# 草稿、回顾草稿）走这条——**允许完整推理**（不做 prefill，不吃 128 上限）。
DEFAULT_REFLECT_MAX_TOKENS = 512
DEFAULT_REFLECT_TIMEOUT_S = 30.0

_REFLECT_SYSTEM = (
    "你是桌宠的快直觉（潜意识推理层）。可以充分思考；最终只输出简洁的中文"
    "正文，不要 JSON、不要解释自己的思考过程。"
)

# ── 摸头/戳 的反应词（快直觉的"其余工作"第一站）──────────────────────
# 壳里的 touch wake 以前唤醒整颗主脑写一句反应，贵且慢。反应词是输出极短
# 的活儿，所以走**短决策**配方（prefill + strict JSON，不思考）：实测
# 1–3 s 出一句；同一需求走思考档要 18 s 而且经常把 256 token 全烧在推理上、
# 正文为空。反应词的"判断"（撒娇/打招呼/不耐烦）交给这段非思考的直觉。
TOUCH_REACTION_MAX_CHARS = 24
TOUCH_REACTION_MAX_TOKENS = 64
TOUCH_REACTION_TIMEOUT_S = 20.0

_TOUCH_PREFILL = '{"reaction": "'
_TOUCH_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {"reaction": {"type": "string"}},
    "required": ["reaction"],
    "additionalProperties": False,
}
_TOUCH_RESPONSE_FORMAT: Dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {"name": "touch_reaction", "strict": True, "schema": _TOUCH_SCHEMA},
}

_TOUCH_SYSTEM = (
    "你是桌宠的快直觉（身体层）。用户（你的主人）刚刚碰了你的身体——是他在碰你，"
    "不是你在碰他。用直觉判断这是撒娇、打招呼，还是不耐烦地戳，然后说出你此刻"
    "**嘴里的一句话**。\n"
    "要求：第一人称说话；不超过 20 个中文字；只写台词。\n"
    "禁止：描写身体动作、表情、眼神、身体部位；不要引号；不要旁白；不要解释。\n"
    "例：主人戳你 3 下（撒娇）→ 诶嘿，好痒～\n"
    "例：主人戳你 7 下（不耐烦）→ 别戳啦，我在呢。\n"
    "例：主人把你拎起来晃了晃（开心）→ 哇——放我下来啦。"
)

# 模型很容易退化成"叙述身体"（左侧躯体瞬间僵硬一下…）。那种句子不该进
# 对话气泡：宁可不出声，交给壳的 touch wake 兜底。
_NARRATION_MARKERS = (
    "眼神", "表情", "身体", "躯体", "抬起头", "低下头", "歪头", "僵硬",
    "颤抖", "缓缓", "瞬间", "耳朵", "尾巴", "眼眶", "嘴角",
)

_TOUCH_REACTION_RE = re.compile(r'"reaction"\s*:\s*"((?:[^"\\]|\\.)*)"')


def build_touch_prompt(
    detail: str,
    *,
    mood: str = "",
    thoughts: Optional[Sequence[str]] = None,
) -> str:
    """Compact Chinese prompt for one touch reaction (never a JSON dump)."""
    lines = [f"身体感受：{str(detail or '').strip() or '（不确定）'}"]
    mood_text = str(mood or "").strip()
    if mood_text:
        lines.append(f"当前心情：{mood_text}")
    recent = [str(t).strip() for t in (thoughts or []) if str(t).strip()][:2]
    if recent:
        lines.append("最近在想：" + "；".join(recent))
    lines.append("直接说一句反应，不要解释。")
    return "\n".join(lines)


_QUOTE_CHARS = "「」『』“”\"'‘’()（）[]【】"


def clean_touch_reaction(text: Any) -> str:
    """One short spoken line: strip quotes/brackets, first sentence, cap length."""
    raw = str(text or "").strip()
    if not raw:
        return ""
    # 热模型爱把台词包在引号里，或先写"（身体反应）"再说话。
    raw = raw.strip(_QUOTE_CHARS).strip()
    # 只取第一句：句末标点之后的内容是对解释/旁白的空间。
    for stop in ("。", "！", "？", "\n", "!", "?"):
        cut = raw.find(stop)
        if 0 < cut < len(raw) - 1:
            raw += stop
            raw = raw[: cut + 1]
            break
    raw = raw.strip(_QUOTE_CHARS).strip()
    if len(raw) > TOUCH_REACTION_MAX_CHARS:
        raw = raw[: TOUCH_REACTION_MAX_CHARS - 1].rstrip() + "…"
    return raw


def _parse_touch_content(content: Any) -> str:
    """Tolerate a JSON continuation, an echoed full object, or bare prose."""
    raw = str(content or "").strip()
    if not raw:
        return ""
    candidates = [_TOUCH_PREFILL + raw]
    if raw.startswith("{"):
        candidates.insert(0, raw)
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except Exception:
            continue
        if isinstance(data, dict) and isinstance(data.get("reaction"), str):
            return data["reaction"]
    match = _TOUCH_REACTION_RE.search(_TOUCH_PREFILL + raw)
    if match:
        try:
            return json.loads('"' + match.group(1) + '"')
        except Exception:
            return match.group(1)
    return raw


async def touch_reaction(
    detail: str,
    config: ReflexConfig,
    *,
    mood: str = "",
    thoughts: Optional[Sequence[str]] = None,
    client: Optional[httpx.AsyncClient] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> dict:
    """One cheap reaction line for a body touch. Never raises.

    Non-thinking prefill + strict JSON (the same lever as judge()): the line
    is ready in ~1–3 s and the model cannot burn the budget on reasoning.
    Returns {"text", "source": "reflex", "latency_ms", "model"} or
    {"text": "", "source": "fallback", "failure"}.
    """
    if not config.enabled:
        return {"text": "", "source": "fallback", "failure": "reflex_disabled"}
    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": _TOUCH_SYSTEM},
            {"role": "user", "content": build_touch_prompt(detail, mood=mood, thoughts=thoughts)},
            # THE lever: continue straight into the JSON string.
            {"role": "assistant", "content": _TOUCH_PREFILL},
        ],
        "response_format": _TOUCH_RESPONSE_FORMAT,
        "max_tokens": TOUCH_REACTION_MAX_TOKENS,
        "temperature": 1.0,
    }
    started = time.perf_counter()
    try:
        if client is not None:
            resp = await client.post(
                config.url.rstrip("/") + REFLEX_ENDPOINT,
                json=payload,
                headers=_auth_headers(config.api_key),
            )
        else:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(TOUCH_REACTION_TIMEOUT_S),
                transport=transport,
                trust_env=False,
            ) as http:
                resp = await http.post(
                    config.url.rstrip("/") + REFLEX_ENDPOINT,
                    json=payload,
                    headers=_auth_headers(config.api_key),
                )
        resp.raise_for_status()
        data = resp.json()
        text = clean_touch_reaction(
            _parse_touch_content(
                data["choices"][0]["message"].get("content") or ""
            )
        )
        if not text:
            return {"text": "", "source": "fallback", "failure": "empty_text"}
        if any(marker in text for marker in _NARRATION_MARKERS):
            # 叙述身体的那种句子不进气泡（见 _NARRATION_MARKERS 注释）。
            return {"text": "", "source": "fallback", "failure": "narration"}
        return {
            "text": text,
            "source": "reflex",
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "model": config.model,
        }
    except Exception as exc:
        logger.debug("touch_reaction fallback", exc_info=True)
        return {
            "text": "",
            "source": "fallback",
            "failure": f"error:{type(exc).__name__}",
        }


async def reflect(
    prompt: str,
    config: ReflexConfig,
    *,
    client: Optional[httpx.AsyncClient] = None,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> dict:
    """Thinking fast-intuition: full reasoning allowed (no prefill).

    The short tick decisions stay on ``judge()`` (non-thinking, prefill "{",
    128 max_tokens, reasoning_tokens=0). Every OTHER small-model job — the
    fast-intuition's actual reasoning work — goes through here with thinking
    enabled. Returns {"text", "source", "latency_ms", "model"} or a
    {"text": "", "source": "fallback", "failure"} on any failure. Never raises.
    """
    if not config.enabled:
        return {"text": "", "source": "fallback", "failure": "reflex_disabled"}
    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": _REFLECT_SYSTEM},
            {"role": "user", "content": str(prompt or "")},
        ],
        "max_tokens": DEFAULT_REFLECT_MAX_TOKENS,
        "temperature": 1.0,
        "stream": False,
    }
    started = time.perf_counter()
    try:
        if client is not None:
            resp = await client.post(
                config.url.rstrip("/") + REFLEX_ENDPOINT,
                json=payload,
                headers=_auth_headers(config.api_key),
            )
        else:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(DEFAULT_REFLECT_TIMEOUT_S),
                transport=transport,
                trust_env=False,
            ) as http:
                resp = await http.post(
                    config.url.rstrip("/") + REFLEX_ENDPOINT,
                    json=payload,
                    headers=_auth_headers(config.api_key),
                )
        resp.raise_for_status()
        data = resp.json()
        text = str(data["choices"][0]["message"].get("content") or "").strip()
        return {
            "text": text,
            "source": "reflex-think",
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "model": config.model,
        }
    except Exception as exc:
        logger.debug("reflect fallback", exc_info=True)
        return {
            "text": "",
            "source": "fallback",
            "failure": f"error:{type(exc).__name__}",
        }


# Cold JIT load is the reflex's real enemy: LM Studio (and llama.cpp servers
# in general) load lazily on the first request of a model, and on a CPU box a
# 1.7B GGUF takes longer than the per-tick timeout — every first tick after a
# boot/模型切换 fell back to quiet and the small model never decided. Prewarm
# is a separate, generous deadline; it runs in the background at boot.
PREWARM_TIMEOUT_S = 180.0


async def prewarm(
    config: ReflexConfig,
    *,
    transport: Optional[httpx.AsyncBaseTransport] = None,
) -> dict:
    """Force the reflex model to be resident before the first life tick.

    Sends the cheapest possible request (one token, no json_schema) and
    returns {"ok": bool, "latency_ms": int, "failure": str}. Never raises.
    """
    if not config.enabled:
        return {"ok": False, "failure": "reflex_disabled"}
    payload = {
        "model": config.model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 1,
        "temperature": 0.0,
        "stream": False,
    }
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(PREWARM_TIMEOUT_S),
            transport=transport,
            trust_env=False,
        ) as http:
            resp = await http.post(
                config.url.rstrip("/") + REFLEX_ENDPOINT,
                json=payload,
                headers=_auth_headers(config.api_key),
            )
        latency_ms = int((time.perf_counter() - started) * 1000)
        if resp.status_code != 200:
            return {"ok": False, "latency_ms": latency_ms,
                    "failure": f"http_{resp.status_code}"}
        return {"ok": True, "latency_ms": latency_ms, "failure": ""}
    except Exception as exc:
        return {
            "ok": False,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "failure": f"error:{type(exc).__name__}",
        }


__all__ = [
    "ACTIONS",
    "DEFAULT_REFLECT_MAX_TOKENS",
    "DEFAULT_REFLECT_TIMEOUT_S",
    "DEFAULT_REFLEX_MAX_TOKENS",
    "DEFAULT_REFLEX_MODEL",
    "DEFAULT_REFLEX_PREFILL",
    "DEFAULT_REFLEX_TIMEOUT_S",
    "DEFAULT_REFLEX_URL",
    "PREWARM_TIMEOUT_S",
    "REFLEX_ENABLED_ENV",
    "REFLEX_MODEL_ENV",
    "REFLEX_URL_ENV",
    "TEMPO_DEFAULT",
    "TEMPO_RANGE_S",
    "TOUCH_REACTION_MAX_CHARS",
    "TOUCH_REACTION_MAX_TOKENS",
    "TOUCH_REACTION_TIMEOUT_S",
    "ReflexConfig",
    "adjust_config",
    "build_touch_prompt",
    "build_user_prompt",
    "clean_touch_reaction",
    "judge",
    "load_reflex_config",
    "prewarm",
    "reflect",
    "touch_reaction",
]
