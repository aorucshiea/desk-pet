"""FastAPI gateway in front of llama.cpp's llama-server.

Exposes the same HTTP/SSE contract the Electron app already speaks with
the legacy PyTorch sidecar, so the renderer (clawd-on-desk/src/pet-chat.*)
does not need to change. The actual inference happens in the subprocess
owned by `LlamaServer`; this file is just glue.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import hashlib
import hmac
import json
import math
import os
import platform
import re
import secrets
import shlex
import shutil
import time
from collections import Counter
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, List, Optional, Union

import httpx

from fastapi import FastAPI, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .clawd_state import ClawdBridge
from .llama_client import LlamaServer, detect_backend, find_sibling_mmproj
from .log_setup import get_logger
from .mcp.mcp_manager import MCPManager
from .mcp.mcp_router import create_mcp_router
from .providers import (
    ProviderRegistry,
    create_providers,
    provider_can_see_images,
    vision_capability_from_config,
)
from .providers.anthropic import AnthropicProvider
from .providers.base import ToolDef, parse_mcp_calls
from .providers.local import LocalProvider
from .providers.openai import OpenAIProvider, is_local_url
from .skills.skill_routes import register_skill_routes
from .omniparser_manager import OmniParserManager
from .memory import MemoryStore, EventStore, MoodStore
from .memory.events import parse_event_block, promote_core
from .memory.tool import (
    MEMORY_TOOL_SCHEMA,
    memory_tool_handler,
)
from .memory import impulse as _impulse_module
from .memory import recall as _recall_module
from .memory import loader as _loader_module
from .memory import continuity as _continuity_module
from .memory import resonance as _resonance_module
from .memory import somatic as _somatic_module
from .memory import search as _memory_search
from .memory import extract_fallback as _extract_fallback
from . import petplugins as _petplugins_module
from . import plugin_policy as _plugin_policy
from . import telemetry
from . import brain as _brain
from .decision import sampler as _decision_sampler


def _sanitize_no_proxy_env() -> None:
    """Normalize bracketed IPv6 entries in NO_PROXY.

    ``NO_PROXY=...,[::1],...`` is a valid shell form but httpx's URL parser
    raises InvalidURL on it, killing the gateway at boot when it builds the
    ClawdBridge client. Strip only the brackets; leave every other entry.
    """
    for key in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(key)
        if not raw or "[" not in raw:
            continue
        parts: list[str] = []
        for part in raw.split(","):
            entry = part.strip()
            if entry.startswith("[") and entry.endswith("]"):
                entry = entry[1:-1]
            if entry:
                parts.append(entry)
        os.environ[key] = ",".join(parts)


_sanitize_no_proxy_env()

# Kernel container mirror: _stream_chat_provider is a MODULE-level function
# and cannot see build_app's closure, so the live PluginManager is mirrored
# here at boot. _organ_service() resolves an organ service through it and
# falls back to the built-in module when the organ is off or absent.
_plugin_container_ref: dict = {}

# Self-evolution service (M1): booted with the plugin container so the
# pet can inspect its own repo, propose edits into an isolated git
# worktree, and evaluate them without touching the running app.
_evolve_service_ref: dict = {}


def _evolve_service():
    return _evolve_service_ref.get("svc")


def _organ_service(name: str, fallback):
    try:
        _mgr = _plugin_container_ref.get("mgr")
        _svc = _mgr.get_service(name) if _mgr else None
    except Exception:
        _svc = None
    return _svc or fallback


def _kernel():
    """Module-level access to the plugin container (None before boot)."""
    return _plugin_container_ref.get("mgr")


# Where the chat pipeline swallows an exception. The file logger runs at INFO,
# so a log.debug() inside `except Exception` is indistinguishable from "never
# happened": the model went a week without seeing a single plugin tool because
# the only trace was a debug line. Count instead, and surface the count.
_silent_failures: dict[str, int] = {}


def _count_failure(site: str) -> int:
    _silent_failures[site] = _silent_failures.get(site, 0) + 1
    return _silent_failures[site]


def _note_failure(site: str, exc: BaseException) -> None:
    """Count a swallowed error and log it at a level that actually reaches disk.

    Throttled to the first occurrence and every 20th: these sit on the chat
    hot path, and an MCP server that stays down would otherwise repeat one
    line per turn forever.
    """
    n = _count_failure(site)
    if n == 1 or n % 20 == 0:
        get_logger().warning("swallowed error at %s (x%d): %s", site, n, exc)


# The provider handling the CURRENT chat turn. Builtin tool handlers live in
# build_app's closure and cannot see _stream_chat_provider's local `provider`;
# a ContextVar keeps the lookup request-scoped, so a user chat and a silent
# narrator turn running concurrently never see each other's provider.
_active_chat_provider_ref: contextvars.ContextVar = contextvars.ContextVar(
    "pet_active_chat_provider", default=None
)


def _active_chat_provider():
    """Provider for the in-flight chat turn, or None outside a chat."""
    return _active_chat_provider_ref.get()


# The ChatRequest of the in-flight chat turn. Builtin tools use it to change
# the rest of THIS round's thinking (pet_enter_slow upgrades req in place);
# the provider loop rebuilds chat_kwargs every iteration from req, so the
# change reaches the model on the next call without a new round.
_active_chat_req_ref: contextvars.ContextVar = contextvars.ContextVar(
    "pet_active_chat_req", default=None
)


# Text of the REAL user message in the current turn. The decision tools feed
# it to Laya as its state, so a silent narrator turn (empty string here)
# never pretends to be something the user said.
_active_user_text_ref: contextvars.ContextVar = contextvars.ContextVar(
    "pet_active_user_text", default=""
)

# Rolls allowed per turn (pet_decision_roll). A model that keeps re-rolling
# instead of committing is stalling, not deciding.
_MAX_DECISION_ROLLS_PER_TURN = 3
_active_roll_count_ref: contextvars.ContextVar = contextvars.ContextVar(
    "pet_active_roll_count", default=0
)

# ReflexConfig of the in-flight chat turn (request-scoped shadow of the boot
# singleton). _stream_chat_provider sits outside build_app's closure, so it
# reads the config through get_reflex_config(); direct generator tests never
# build an app, and a per-request override beats the boot value for exactly
# that turn — the same pattern as _active_chat_provider_ref.
_active_reflex_config_ref: contextvars.ContextVar = contextvars.ContextVar(
    "pet_active_reflex_config", default=None
)


def _active_user_text() -> str:
    """Current turn's user text, or "" outside a real user turn."""
    return _active_user_text_ref.get() or ""


def _screen_tool_can_see_images(server) -> bool:
    """Vision capability for screen tools.

    Prefer the active chat provider: a cloud provider can see images even
    when the local llama-server has no mmproj. Outside a chat turn (or when
    resolution failed) fall back to the local projector check.
    """
    try:
        return provider_can_see_images(_active_chat_provider(), server)
    except Exception:
        return bool(getattr(server, "mmproj_path", None))


# ── Vision fallback channel ────────────────────────────────────────────
# The default chat model is a text-only browser bridge, so the model cannot
# consume an image even though the user wants the pet to "see". Route the
# screenshot through a separate OpenAI-compatible vision endpoint and hand
# the model a description. Selection order: PET_VISION_PROVIDER, then
# "deepseek", then common Chinese vision providers.
_VISION_PROVIDER_ENV = "PET_VISION_PROVIDER"
_VISION_PROVIDER_FALLBACKS = ("deepseek", "硅基流动", "siliconflow")

_SCREEN_DESCRIBE_PROMPT = (
    "这是用户当前屏幕的截图。请用中文客观、详细地描述：应用/窗口、"
    "画面布局、重要文字、用户在做什么；不要猜测，不要省略重要文字。"
)
_SCREEN_DESCRIBE_SYSTEM = (
    "你是一个屏幕视觉描述器。只根据截图中可见的内容如实描述，"
    "不要编造看不到的信息；保留重要的界面文字、标题和报错信息。"
)
_SCREEN_DESCRIBE_TIMEOUT = 90.0


def _select_vision_provider(active, registry, server=None, env=None):
    """Pick a provider that can actually see images.

    Returns ``active`` when it is vision-capable; otherwise a non-active
    OpenAI-compatible provider from the registry. ``None`` means there is
    no vision channel at all (callers must degrade to observe_screen).
    """
    try:
        if active is not None and provider_can_see_images(active, server):
            return active
    except Exception:
        pass

    names: list[str] = []
    source = env if env is not None else os.environ
    preferred = str(source.get(_VISION_PROVIDER_ENV) or "").strip() or "deepseek"
    names.append(preferred)
    names.extend(_VISION_PROVIDER_FALLBACKS)

    seen: set[str] = set()
    for name in names:
        if not name or name in seen:
            continue
        seen.add(name)
        try:
            candidate = registry.get_or(name)
        except Exception:
            candidate = None
        if candidate is None or candidate is active:
            continue
        if not isinstance(candidate, OpenAIProvider):
            continue
        try:
            if provider_can_see_images(candidate, server):
                return candidate
        except Exception:
            continue
    return None


async def _describe_screen_with_provider(provider, b64: str, *, timeout: float = _SCREEN_DESCRIBE_TIMEOUT) -> str:
    """Ask a vision provider to describe a base64 JPEG screenshot.

    Streaming deltas are joined; ``think`` chunks are ignored. Any transport
    error, provider error event, timeout or empty answer raises RuntimeError
    with a user/model-readable message.
    """
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": _SCREEN_DESCRIBE_PROMPT},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/jpeg;base64," + b64},
            },
        ],
    }]
    parts: list[str] = []
    errors: list[str] = []

    async def _collect() -> None:
        async for chunk in provider.chat(
            messages,
            system=_SCREEN_DESCRIBE_SYSTEM,
            tools=None,
            max_tokens=900,
            temperature=0.2,
            top_p=0.9,
            thinking_override=False,
            reasoning_effort_override=None,
        ):
            if not isinstance(chunk, dict):
                continue
            kind = chunk.get("type")
            if kind == "delta":
                text = chunk.get("content") or ""
                if text:
                    parts.append(str(text))
            elif kind == "error":
                errors.append(str(chunk.get("message") or "vision provider error"))
            # "think" and every other event type are intentionally ignored.

    try:
        await asyncio.wait_for(_collect(), timeout=timeout)
    except asyncio.TimeoutError:
        raise RuntimeError(f"视觉通道超时（{timeout:.0f} 秒）") from None
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"视觉通道调用失败：{exc}") from exc

    if errors:
        raise RuntimeError(errors[0])
    text = "".join(parts).strip()
    if not text:
        raise RuntimeError("视觉通道没有返回内容")
    return text

from .memory.recall import (
    RECALL_TOOL_SCHEMA,
    recall_tool_handler,
    get_event_store,
    get_session_recall_count,
    set_event_store,
)
from .screen_consent import ScreenPermissionManager
from .holo_agent import HoloRunner, probe as holo_probe
from .memory_context import MemoryContext, theme_slug
from .memory.mood import build_mood_assessment_prompt, EMOTION_TO_MOOD

# Timestamp of the most recent conversation — the decay organ freezes when
# the user hasn't talked to the pet for FREEZE_HOURS (memory only fades
# while the pet is awake). Updated at the start of each /api/chat stream.
# Shared as a cell (not a bare global) because it is also a KERNEL SERVICE:
# the organ_decay / organ_dream plugins inject "conversation_state" and read
# this dict, so the chat path and the organs see the same truth.
_conversation_state = {"last_conversation_at": None}

# Thread-safe submit of coroutines onto the main asyncio loop. Plugins load
# (and hot-reload) from the watcher thread, and the provider clients are
# loop-bound — so organ plugins that need async submit through this service
# instead of spawning their own loop.
_main_loop_holder: dict = {}


def _schedule_on_main_loop(coro):
    loop = _main_loop_holder.get("loop")
    if loop is None or loop.is_closed():
        coro.close()
        return None
    return loop.call_soon_threadsafe(loop.create_task, coro)

# Death-note hook (死亡遗嘱): set by build_app so the module-level chat
# stream can persist memories even when the client dies mid-stream —
# the renderer is the usual extraction trigger, but the renderer is
# also the thing most likely to vanish. Signature: fn(reply_text) -> dict.
_extraction_hook = None
# Strong references for fire-and-forget extraction retries (loop alone is not
# enough: Python only keeps weak references to scheduled tasks).
_fallback_tasks: set[asyncio.Task] = set()


def _fallback_task_done(task: asyncio.Task) -> None:
    _fallback_tasks.discard(task)
    if task.cancelled():
        return
    try:
        exc = task.exception()
    except Exception as err:
        get_logger().warning("memory extraction fallback inspection failed: %s", err)
        return
    if exc is not None:
        telemetry.inc("memory.extraction.fallback_errors")
        get_logger().warning("memory extraction fallback failed: %s", exc)


def _cancel_pending_fallbacks() -> None:
    """Give a new real user turn priority over background extraction.

    The fallback may be generating on the same single-slot local engine the
    user is now waiting for. Cancelling is safe: the task checkpoints at its
    ``await`` points, and ``apply_extraction`` is synchronous once it starts
    writing.
    """
    for task in list(_fallback_tasks):
        if not task.done():
            task.cancel()


# Consecutive proactive requests immediately preceding the current one
# (a proactive request = the last user message is the renderer's
# [系统提示：…] wake-up prompt). Feeds the speech-impulse backoff: the
# longer the pet has been talking into the void, the longer the
# subconscious waits before trying again. Reset the moment the user
# actually speaks.
_proactive_streak = 0

# Marker the renderer uses for proactive wake-up prompts ([系统提示：…]).
_PROACTIVE_PROMPT_PREFIX = "[系统提示"

# ── 身体感受 (somatic sense) ─────────────────────────────────────────────
# The pet BODY (Electron shell) reports touch: drags, click bursts. One
# process-wide buffer shared by the reporting endpoint (writer) and the
# chat stream (reader) — module scope because _stream_chat_provider sits
# outside build_app's closure (the event_store NameError lesson).
_somatic_buffer = _somatic_module.SensationBuffer()

# ── 决策层（2026-10-19）：瞬时记忆 / 直觉注册表 / 身体日志 / 决策循环 ──
# 模块级引用：lifespan 里创建，_stream_chat_provider 里注入 system。
_ephemeral_ref = None
_intuition_registry_ref = None
_body_log_ref = None
_decision_loop_ref = None
_last_user_msg = ""
_last_user_ts = 0.0
# Real user turn only (proactive wake-up prompts are excluded): a voice
# older than this is stale, so intuition pressure must not shorten gaps.
_last_real_user_ts = 0.0
from . import screen_click
from .screen_capture import capture as screen_capture
from .think_filter import (
    ThinkBlockFilter,
    ControlTagFilter,
    ToolNarrationFilter,
    DStateFilter,
)
from .updater import DEFAULT_SOURCE as DEFAULT_UPDATE_SOURCE
from .updater import ModelUpdater
from .sidecar_updater import SidecarUpdater
from .decision import cortex as _cortex
from .decision import reflex as _reflex_module
from .decision.laya_router import BudgetDecision
from .vitals import Vitals as _Vitals
from .ledger import Ledger as _Ledger


# ── D 核心（快直觉）────────────────────────────────────────────────
# Per-theme DStateStore provider, installed by build_app. Direct generator
# tests never install one, so their legacy behaviour is untouched.
_dstate_provider = None


def set_dstate_provider(provider) -> None:
    """Install (or reset, with None) the per-theme D state provider."""
    global _dstate_provider
    _dstate_provider = provider


# The passive D gate runs before every provider call, including boot. On CPU
# a three-question Laya round measures ~1.4-1.9s idle and 5s+ when another
# local model saturates the machine, so the sampler's 0.45s default always
# timed out and silently fell back to priors. Autonomous rounds can afford a
# long budget (they are hidden behind the life-loop floor); a direct user
# turn keeps the fast-intuition budget and falls back to priors when the
# machine is busy.
def _cortex_timeout_env(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if raw:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if 0.05 <= value <= 60.0:
            return value
    return default


CORTEX_DECISION_TIMEOUT = _cortex_timeout_env(
    "PET_CORTEX_DECISION_TIMEOUT", 6.0
)
CORTEX_DECISION_TIMEOUT_USER = _cortex_timeout_env(
    "PET_CORTEX_DECISION_TIMEOUT_USER", 2.5
)


# Brain stream: providers stream reasoning as tiny token deltas. Publishing
# one "思考" card per fragment floods the feed, so coalesce until a chunk is
# readable (or a burst pause) and flush the rest when the stream ends.
# A silent safety fallback looks like a lazy pet, not a broken engine.
# Alert once per failure streak so the brain feed explains the quiet.
_D_INTUITION_ALERT = {"streak": 0}

# Reflex time-sense latch: only the small model's own previous `time_relevant`
# may unlock the clock for the NEXT tick. The grant is one round late by
# design — the model asks this tick, the system answers the next one — so the
# flag must survive the round: it cannot live in the per-round `_reflex_decision`
# local (that was reset to None before the check and the loop never closed).
_REFLEX_TIME_STATE = {"wanted": False, "asked_at": 0.0}

THINK_EVENT_MIN_CHARS = 600
THINK_EVENT_MIN_SECS = 5.0


def _cortex_round_timeout(store, proactive: bool) -> float:
    if not proactive:
        return CORTEX_DECISION_TIMEOUT_USER
    if (os.environ.get("PET_CORTEX_DECISION_TIMEOUT") or "").strip():
        return CORTEX_DECISION_TIMEOUT
    return min(30.0, CORTEX_DECISION_TIMEOUT + 4.5 * len(store.questions))


def _cortex_enabled() -> bool:
    raw = (os.environ.get("PET_CORTEX_ENABLED") or "").strip().lower()
    return raw not in ("0", "false", "no", "off", "disabled")


def _get_dstate_store():
    """Current theme's D store, or None outside the app / on any failure."""
    provider = _dstate_provider
    if provider is None:
        return None
    try:
        return provider()
    except Exception as exc:
        get_logger().debug("cortex: D state provider failed: %s", exc)
        return None


# ── 生命体征（电量/血量/睡眠）+ 500 轮台账 ─────────────────────────────────
# build_app boots BOTH once, beside the memory stores (vitals.json /
# ledger.json live next to events.json in the CURRENT theme's dir — the
# theme the soul is wearing owns its own life state). _stream_chat_provider
# sits outside build_app's closure, so accessors mirror the kernel-service
# pattern (_plugin_container_ref / _evolve_service_ref): module scope,
# request-free, None before boot. Direct generator tests that never build
# the app keep their legacy behaviour: no vitals object → no drain, no
# ledger tick, no short-circuit.
_vitals_ref: dict = {}
_ledger_ref: dict = {}


def set_vitals(v) -> None:
    """Install (or reset, with None) the process vitals singleton."""
    if v is None:
        _vitals_ref.pop("svc", None)
    else:
        _vitals_ref["svc"] = v


def set_ledger(v) -> None:
    """Install (or reset, with None) the process ledger singleton."""
    if v is None:
        _ledger_ref.pop("svc", None)
    else:
        _ledger_ref["svc"] = v


def get_vitals():
    """The vitals singleton (电量/血量/睡眠), or None outside a built app."""
    return _vitals_ref.get("svc")


def get_ledger():
    """The ledger singleton (500 轮台账), or None outside a built app."""
    return _ledger_ref.get("svc")


# ── 快直觉 reflex 配置（System 0 的请求设置）────────────────────────────────
# build_app 启动时加载一次（env > providers.json "reflex" > defaults），存成
# 模块级单例 —— 与 get_vitals()/get_ledger() 同一模式：_stream_chat_provider
# 在 build_app 闭包之外，访问配置走模块级。None（未构建 / 加载失败）时聊天
# 流按"reflex 层缺席"处理，保持旧 D-quiet 行为，绝不影响出话。
_reflex_config_ref: dict = {}


def set_reflex_config(cfg) -> None:
    """Install (or reset, with None) the process reflex-config singleton."""
    if cfg is None:
        _reflex_config_ref.pop("cfg", None)
    else:
        _reflex_config_ref["cfg"] = cfg


def get_reflex_config():
    """ReflexConfig for the in-flight turn — the request-scoped shadow ONLY.

    build_app parses the boot singleton from providers.json, but the gate
    must never arm outside a real routed chat: direct `_stream_chat_provider`
    calls (tests, internal tools) stay in the legacy world unless something
    explicitly sets the override. The /api/chat route seeds the shadow from
    the boot singleton for its request (anyio's start_soon copies the
    caller's context into the streaming task).
    """
    return _active_reflex_config_ref.get()


def set_reflex_config_override(cfg) -> None:
    """Arm (or disable with a disabled ReflexConfig) the reflex gate for
    direct `_stream_chat_provider` calls — no app boot needed."""
    _active_reflex_config_ref.set(cfg)


def clear_reflex_config_override() -> None:
    """Drop the request-scoped override; the boot singleton wins again."""
    _active_reflex_config_ref.set(None)


def _mood_label_for_reflex(emotion_index: float) -> str:
    """Cheap mood word for the reflex state line (情绪指数 → 粗粒度心情词).

    Same threshold family as the emotion temperature modulation — a few
    comparisons, never a store read. Unknown/neutral lands on 平静.
    """
    try:
        idx = float(emotion_index)
    except (TypeError, ValueError):
        return "平静"
    if idx >= 0.3:
        return "开心"
    if idx >= 0.1:
        return "愉快"
    if idx <= -0.3:
        return "难过"
    if idx <= -0.1:
        return "低落"
    return "平静"


def _vitals_state_dir():
    """Directory the vitals/ledger singletons were built in (for
    graveyard_archive from the module-level chat stream; None pre-boot)."""
    return _vitals_ref.get("dir")


# Pending round-state slot set by the finally block after ledger.round_tick()
# and consumed by the NEXT round: organize_kind (audit/recap) + the
# slow-overflow latch. Keys absent/false ⇒ the next round is a normal one.
_pending_vitals_round: dict = {"organize_kind": None, "overflow": False}

# B-17 双睡眠统一：上次推送给壳的 vitals 睡眠态（None=未知，True=睡着，False=清醒）
_last_vitals_sleep: Optional[bool] = None

# Fire-and-forget boot tasks (reflex 预热等) 需要强引用：裸 create_task 返回的
# Task 只被弱引用持有，事件循环空转时可能被 GC，协程就悄无声息地不再执行。
_BOOT_TASKS: "set" = set()

# 触摸反应词的节流闩：一次抚摸/连戳只出一句，别把身体变成话痨。
_TOUCH_REACTION_STATE: dict = {"last_at": 0.0}
_TOUCH_REACTION_MIN_GAP_S = 15.0

# 记忆整理轮（点 7）：audit 只保留核心记忆工具；recap 一个工具都不给。
_ORGANIZE_AUDIT_TOOLS = {"memory", "recall", "memory_search"}
_ORGANIZE_PROMPTS = {
    "audit": (
        "【系统提示：这是你的记忆整理时刻。全面检视你的长期记忆——事件、记忆文件夹、"
        "核心记忆——每一处角落，确认它们仍然是你期望的样子。发现错误就修正，"
        "发现遗忘就补写。这轮只做整理，不要调用屏幕或桌面工具。】"
    ),
    "recap": (
        "【系统提示：这是你的回顾时刻。下面是最近 100 轮的生命台账（快直觉的一行"
        "记录与主脑轮的完整记录混排）。用一段话向自己总结这 100 轮里发生了什么——"
        "你做了什么、想到了什么、有什么悬而未决。只总结，不要调用工具。】"
    ),
}


def _recent_ledger_lines(lg, n: int = 100) -> list[str]:
    """Last ``n`` ledger entries as compact recap material, newest last.

    One line per entry: fast → "[快] text HH:MM" (local clock), slow /
    organize → "[主脑]" / "[整理]" + first line of the text. Every line is
    capped (100 chars) so 100 entries can never blow up the prompt. Returns
    [] for a missing ledger; never raises.
    """
    if lg is None:
        return []
    try:
        entries = lg.entries()
    except Exception:
        return []
    lines: list[str] = []
    for entry in entries[-max(1, int(n)):]:
        kind = str(entry.get("kind") or "")
        text = str(entry.get("text") or "").strip()
        ts = str(entry.get("ts") or "")
        try:
            clock = datetime.fromisoformat(ts).astimezone().strftime("%H:%M")
        except Exception:
            clock = ts[11:16]  # raw ISO slice; better than nothing
        first = text.splitlines()[0].strip() if text else ""
        first = first[:100]
        if kind == "fast":
            lines.append(f"[快] {first} {clock}".strip())
        elif kind == "slow":
            lines.append(f"[主脑] {first}".strip())
        elif kind == "organize":
            lines.append(f"[整理] {first}".strip())
        else:
            lines.append(f"[其他] {first}".strip())
    return lines


def _default_memory_root() -> Path:
    """Boot-fallback state dir for graveyard_archive (no built app)."""
    root = os.environ.get("PET_MEMORY_DIR") or str(Path.home() / ".pet")
    return Path(root)


def _vitals_health() -> dict:
    """vitals object for /api/health — None fields when the system is off."""
    v = get_vitals()
    if v is None:
        return None
    try:
        # 当前节奏（ tempo store 可能过期，过期则为 derive 的 slow）
        tempo_mode = None
        try:
            tempo_mode, _tempo_source = _resolve_tempo(time.time())
        except Exception:
            tempo_mode = None

        # 睡眠剩余秒数与是否强制
        sleep_remaining_s = None
        sleep_forced = None
        try:
            if v.is_asleep():
                sleep_remaining_s = max(0, int(v.sleep_remaining_s()))
                sleep = v.sleep
                sleep_forced = bool(sleep and sleep.get("decided_by") == "forced")
        except Exception:
            pass

        return {
            "battery_percent": round(float(v.percent()), 1),
            "forced_off": bool(v.forced_off),
            "should_warn": bool(v.should_warn()),
            "hp": round(float(v.hp), 1),
            "died": bool(v.died),
            "asleep": bool(v.is_asleep()),
            "sleep_stage": v.sleep_stage,
            "tempo_mode": tempo_mode,
            "sleep_remaining_s": sleep_remaining_s,
            "sleep_forced": sleep_forced,
        }
    except Exception as exc:
        get_logger().warning("vitals health read failed: %s", exc)
        return None


def _ledger_health() -> dict:
    """ledger object for /api/health — None fields when the system is off."""
    l = get_ledger()
    if l is None:
        return None
    try:
        return {
            "round_count": int(l.round_count()),
            "counts": l.counts(),
            "pending_recap": bool(l.pending_recap),
        }
    except Exception as exc:
        get_logger().warning("ledger health read failed: %s", exc)
        return None


def _should_archive_death(v) -> bool:
    """True exactly once per death: died, and not archived this process yet."""
    if v is None or not bool(v.died):
        return False
    try:
        if getattr(v, "_gateway_archived", False):
            return False
        v._gateway_archived = True  # one-way process-local flag
        return True
    except Exception:
        return True


# ── Request / response shapes ────────────────────────────────────────────────


class ChatMessage(BaseModel):
    role: str = Field(..., description="'system' | 'user' | 'assistant'")
    # OpenAI vision-compatible: content may be a plain text string OR a
    # list of content blocks, e.g.
    #   [{"type":"text","text":"..."},
    #    {"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}]
    # We forward the raw value into the gateway so multimodal models
    # (via --mmproj) actually receive the image payload. Plain-text
    # callers are unaffected: pydantic accepts the str form verbatim.
    content: Union[str, List[dict]]


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    max_new_tokens: int = 4096
    temperature: float = 0.6
    top_p: float = 0.95
    top_k: int = 0
    repetition_penalty: float = 1.05
    stream: bool = True
    system: Optional[str] = None
    thinking: bool = False
    # Gateway-internal only: Laya fills this per turn. Electron does not need
    # to send it; OpenAIProvider gets it through reasoning_effort_override.
    reasoning_effort: Optional[str] = None
    silent: bool = False  # bypass pet state pushes (used by narrator)
    # When true the gateway sends `lora: []` to llama-server for THIS
    # request only, which disables every pre-loaded LoRA adapter for the
    # current generation without touching global scales. Used by the
    # narrator so its informational replies don't pick up the active
    # persona's stylistic bias. No-op when no adapter is currently
    # active.
    disable_adapter: bool = False
    # Model provider routing (Phase 2)
    model_provider: Optional[str] = Field(
        default=None,
        description="'local' | 'openai' | 'anthropic' | None (=local fallback)",
    )
    tools_enabled: bool = Field(
        default=True,
        description="When false, skip MCP tool injection and detection",
    )
    context_window: Optional[int] = None
    # The browser-web provider pays for the whole system prompt every turn;
    # its shell sends this to request the compact body block (no window-body
    # vision-cone prose; screenshots go through capture_screen attachments).
    compact_prompt: bool = False


class PetInteractionRequest(BaseModel):
    """One body interaction reported by the Electron shell (the BODY).

    kind="drag": the user picked the pet up and moved it — distance_px is
    the straight-line window displacement, duration_ms the gesture length.
    kind="click": a settled click burst — clicks is the burst count
    (1 = a tap, 2-3 = poking, 4+ = relentless poking).
    """
    kind: str
    clicks: int = 0
    distance_px: int = 0
    duration_ms: int = 0


# When thinking=true the model emits a <think> block before the
# answer; both share one max_new_tokens budget. Bump the floor so reasoning
# doesn't eat the entire allowance and truncate the reply.
THINKING_MIN_MAX_NEW_TOKENS = 1280
MAX_NEW_TOKENS_CAP = 8192


def _effective_max_new_tokens(req: ChatRequest) -> int:
    base = int(max(1, min(req.max_new_tokens, MAX_NEW_TOKENS_CAP)))
    if req.thinking:
        return min(MAX_NEW_TOKENS_CAP, max(base, THINKING_MIN_MAX_NEW_TOKENS))
    return base


def _output_budget(provider, req: ChatRequest, decision=None) -> int:
    """Output-token budget for one provider.

    When Laya produced a ``BudgetDecision`` for this turn, that decision is
    authoritative: cloud endpoints no longer get an unconditional 8192
    floor. The absolute cap still applies, and the router has already
    raised tool turns to at least 1024 tokens.

    Without a decision (silent/proactive turns or a router crash) the old
    behaviour stands. Cloud APIs keep a generous 8192 floor (tool-call
    turns + long replies). Local OpenAI-compatible engines (LM Studio /
    llama.cpp / Ollama) must NOT get that floor: LM Studio JIT-loads the
    model with ``prompt_tokens + max_tokens`` of context, so an 8192 output
    budget on top of a 10k-token pet prompt balloons the KV cache, slows
    prefill and makes a 2B model look "stuck". The renderer already asks for
    a sane budget (768, or 1280 with thinking); use it as-is.
    """
    if decision is not None:
        try:
            routed = int(decision.max_tokens)
        except (TypeError, ValueError, AttributeError):
            routed = 0
        if routed > 0:
            return max(1, min(routed, MAX_NEW_TOKENS_CAP))

    effective = _effective_max_new_tokens(req)
    if getattr(provider, "is_local", False) or provider.name == "local":
        return effective
    return max(effective, 8192)


def _estimate_tokens(text: str) -> int:
    """Very rough token estimate (CJK ~1/char, other ~4 chars/token)."""
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        cp = ord(ch)
        if 0x3000 <= cp <= 0x9FFF or 0xAC00 <= cp <= 0xD7A3 or 0xFF00 <= cp <= 0xFFEF:
            cjk += 1
        else:
            other += 1
    return cjk + (other + 3) // 4


# ── Emotion index → temperature (情绪指数温度调控) ─────────────────────
# The pet's emotion index (calm=0, happy +0.1, sad -0.1, …) modulates the
# generation temperature: happy → more divergent/creative, sad → more
# subdued. Pure function so it's unit-testable.
TEMP_BASE_MOD = 0.3
TEMP_MIN = 0.2
TEMP_MAX = 1.5


def modulate_temperature(base: float, emotion_index: float) -> float:
    """base + index*0.3, clamped to [0.2, 1.5]. index=0 → base unchanged."""
    return max(TEMP_MIN, min(TEMP_MAX, float(base) + float(emotion_index) * TEMP_BASE_MOD))


# ── Model discovery ─────────────────────────────────────────────────────────


def _is_mmproj(name: str) -> bool:
    """mmproj-*.gguf files are vision projector weights, NOT main models.
    Loading one as --model fails ("unsupported model architecture:
    'clip'"), so they must never surface in model discovery."""
    return name.lower().startswith("mmproj")


def discover_models(roots: List[Path]) -> List[dict]:
    """Return [{name, path}] for every *.gguf file under `roots`.

    Excludes mmproj-* vision-projector files (they are --mmproj weights,
    not loadable as a main model).
    """
    seen: set[Path] = set()
    out: List[dict] = []
    for root in roots:
        try:
            r = root.expanduser().resolve()
        except Exception:
            continue
        if not r.exists() or r in seen:
            continue
        seen.add(r)
        if r.is_file() and r.suffix.lower() == ".gguf" and not _is_mmproj(r.name):
            out.append({"name": r.name, "path": str(r)})
            continue
        if not r.is_dir():
            continue
        for p in sorted(r.rglob("*.gguf")):
            if any(part.endswith(".update-staging") or part.endswith(".bak") for part in p.parts):
                continue
            if _is_mmproj(p.name):
                continue
            out.append({"name": p.name, "path": str(p)})
    return out


def _default_model_roots() -> List[Path]:
    """Locations to scan for *.gguf when no explicit PET_MODEL_DIR
    is set. The Electron host passes `--model` explicitly so this is
    only used by direct CLI / dev runs."""
    here = Path(__file__).resolve().parent.parent
    return [
        Path.home() / "Library" / "Application Support" / "Clawd on Desk" / "models",
        Path.home() / ".local" / "share" / "Clawd on Desk" / "models",
        here / "models",
        here.parent / "models",
    ]


# ── LoRA adapter discovery ──────────────────────────────────────────────────


# Filename-keyword → persona slug. The slug is the stable identifier the
# Electron renderer keys off ("default" / "neko" / "muice" / ...) when
# deciding things like whether to flip `thinking` off (persona LoRAs don't
# carry <think> training, so reasoning collides with their style).
# Matching is substring + case-insensitive against the filename stem.
PERSONA_HINTS: dict[str, str] = {
    "nekoqa": "neko",
    "neko": "neko",
    "muice": "muice",
    "chuuni": "chuuni",
    "moyu": "moyu",
    "zhiyuan": "zhiyuan",
}


def _persona_for(path: Path) -> str:
    stem = path.stem.lower()
    parent = path.parent.name.lower()
    haystack = f"{parent}/{stem}"
    for needle, slug in PERSONA_HINTS.items():
        if needle in haystack:
            return slug
    return "custom"


def _default_adapter_roots() -> List[Path]:
    """Where to scan for `*.gguf` LoRA adapters when no `PET_ADAPTER_DIR`
    env is set. The Electron host normally injects that env, so this only
    runs for direct CLI / dev / test invocations.

    The order here mirrors `_default_model_roots`: per-user app data first,
    then the dev-only repo path next to the sidecar package.
    """
    here = Path(__file__).resolve().parent.parent
    return [
        Path.home() / "Library" / "Application Support" / "Clawd on Desk" / "adapters",
        Path.home() / ".local" / "share" / "Clawd on Desk" / "adapters",
        here.parent / "adapters",   # <repo>/adapters/ in dev checkouts
    ]


def discover_adapters(roots: List[Path]) -> List[dict]:
    """Return [{name, path, persona}] for every `*.gguf` LoRA under `roots`.

    Skips electron-builder staging / backup directories the same way
    `discover_models` does, so we don't accidentally surface half-downloaded
    adapters."""
    seen_files: set[Path] = set()
    out: List[dict] = []
    for root in roots:
        try:
            r = root.expanduser().resolve()
        except Exception:
            continue
        if not r.exists() or not r.is_dir():
            continue
        for p in sorted(r.rglob("*.gguf")):
            if any(part.endswith(".update-staging") or part.endswith(".bak") for part in p.parts):
                continue
            try:
                resolved = p.resolve()
            except Exception:
                continue
            if resolved in seen_files:
                continue
            seen_files.add(resolved)
            out.append({
                "name": p.name,
                "path": str(p),
                "persona": _persona_for(p),
            })
    return out


def _resolve_adapter_root(initial_model: Optional[Path]) -> Optional[Path]:
    """Pick the canonical writable adapter dir for `/api/load-adapter`
    "open in Finder" hints. Resolution order:

    1. `PET_ADAPTER_DIR` env (Electron host injects this in packaged
       mode pointing at `<userData>/adapters/`)
    2. First default root that already exists
    3. First default root regardless of existence (the caller can then
       `mkdir -p` before opening Finder)
    """
    env_dir = os.environ.get("PET_ADAPTER_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    for cand in _default_adapter_roots():
        if cand.exists() and cand.is_dir():
            return cand
    defaults = _default_adapter_roots()
    return defaults[-1] if defaults else None


# Mirror file Electron writes after every manifest mutation. Lives in
# the adapter dir under a dot prefix so `discover_adapters`'s `*.gguf`
# scan misses it. Schema mirrors `<userData>/pet-adapters.json` 1:1
# (see clawd-on-desk/src/pet-chat.js).
_MANIFEST_MIRROR = ".manifest.json"


def read_adapter_manifest(adapter_root: Optional[Path]) -> dict:
    """Return the parsed `.manifest.json` from `adapter_root`, or an
    empty manifest if the file is absent / malformed.

    The gateway is a pure reader here — Electron owns the data and
    re-writes the mirror on every CRUD operation. Reading on every
    `/api/adapters` request keeps us a snapshot fresh without an
    explicit refresh endpoint."""
    if adapter_root is None:
        return {"version": 1, "items": []}
    try:
        mirror = Path(adapter_root) / _MANIFEST_MIRROR
        if not mirror.is_file():
            return {"version": 1, "items": []}
        with mirror.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"version": 1, "items": []}
    if not isinstance(data, dict):
        return {"version": 1, "items": []}
    items = data.get("items") if isinstance(data.get("items"), list) else []
    return {"version": int(data.get("version") or 1), "items": items}


def _manifest_by_resolved_path(manifest: dict) -> dict[Path, dict]:
    """Index manifest items by their resolved absolute path so the
    `/api/adapters` merge step is O(1) per scanned file."""
    out: dict[Path, dict] = {}
    for entry in manifest.get("items", []) or []:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("path")
        if not isinstance(raw, str) or not raw:
            continue
        try:
            out[Path(raw).expanduser().resolve()] = entry
        except Exception:
            continue
    return out


# ── App factory ──────────────────────────────────────────────────────────────


def build_app(
    *,
    initial_model: Optional[Path],
    update_source: str = DEFAULT_UPDATE_SOURCE,
    ctx_size: int = 131072,
    n_gpu_layers: int = -1,
    threads: Optional[int] = None,
) -> FastAPI:
    log = get_logger()
    bridge = ClawdBridge(enabled=True, debug=False)

    # Resolve the adapter root so /api/adapters can scan it; we still
    # show the full list to the UI even when none are loaded yet, so
    # users can browse + activate any LoRA from Settings.
    adapter_root = _resolve_adapter_root(initial_model)

    # Boot-time LoRA load is now *opt-in*: only the LoRA the Electron
    # host has persisted as the active one (env PET_ACTIVE_ADAPTER)
    # gets passed to llama-server via --lora. Default behaviour is pure
    # Base — no third-party LoRA is preloaded just because it happens
    # to live on disk. Switching to a different LoRA later triggers
    # `LlamaServer.reload_adapters([new])`, costing one llama-server
    # restart but keeping the steady-state memory minimal.
    _env_active = os.environ.get("PET_ACTIVE_ADAPTER", "").strip()
    initial_active: Optional[Path] = None
    if _env_active:
        try:
            cand = Path(_env_active).expanduser().resolve(strict=True)
            if cand.suffix.lower() == ".gguf":
                initial_active = cand
            else:
                log.warning("PET_ACTIVE_ADAPTER ignored (not .gguf): %s", cand)
        except FileNotFoundError:
            log.warning("PET_ACTIVE_ADAPTER points at missing file: %s", _env_active)

    def _env_int(name: str) -> Optional[int]:
        raw = (os.environ.get(name) or "").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            log.warning("%s=%r is not an int; ignoring", name, raw)
            return None

    def _env_bool(name: str) -> Optional[bool]:
        raw = (os.environ.get(name) or "").strip().lower()
        if raw in ("1", "true", "on", "yes"):
            return True
        if raw in ("0", "false", "off", "no"):
            return False
        return None

    server = LlamaServer(
        model_path=initial_model,
        ctx_size=ctx_size,
        n_gpu_layers=n_gpu_layers,
        threads=threads,
        adapters=[initial_active] if initial_active else [],
        # Boot-time multimodal pairing: picked up if the bundled model
        # ships with a corresponding `mmproj-*.gguf` in the same dir.
        mmproj_path=find_sibling_mmproj(initial_model) if initial_model else None,
        # Advanced runtime knobs, injected by the Electron host from
        # Settings → Engine → Advanced parameters. Defaults keep the
        # engine's own behaviour except load-mode, which we pin to mmap
        # (a 20 GB+ checkpoint must not be read into RAM wholesale).
        load_mode=(os.environ.get("PET_LOAD_MODE") or "mmap").strip() or "mmap",
        cache_type_k=(os.environ.get("PET_CACHE_TYPE_K") or "").strip() or None,
        cache_type_v=(os.environ.get("PET_CACHE_TYPE_V") or "").strip() or None,
        flash_attn=_env_bool("PET_FLASH_ATTN"),
        n_cpu_moe=_env_int("PET_N_CPU_MOE"),
        batch_size=_env_int("PET_BATCH_SIZE"),
        ubatch_size=_env_int("PET_UBATCH_SIZE"),
        parallel=_env_int("PET_PARALLEL"),
        ctx_checkpoints=_env_int("PET_CTX_CHECKPOINTS"),
        kv_offload=_env_bool("PET_KV_OFFLOAD"),
        kv_unified=_env_bool("PET_KV_UNIFIED"),
        spec_type=(os.environ.get("PET_SPEC_TYPE") or "").strip() or None,
        spec_draft_n_max=_env_int("PET_SPEC_DRAFT_N_MAX"),
        verbosity=_env_int("PET_VERBOSITY"),
        # Free-form pass-through: PET_EXTRA_ARGS="--main-gpu 0 --tensor-split 1,0"
        # (space separated; quoted segments survive).
        extra_args=shlex.split(os.environ.get("PET_EXTRA_ARGS") or ""),
    )

    # In-memory adapter state. Single source of truth for what the
    # Electron app sees as "the active LoRA". Boots from the persisted
    # choice; cleared on /api/load-adapter {path:null}; updated to a
    # new path on /api/load-adapter {path:<gguf>}. The Electron host
    # is responsible for writing the latest choice back to its prefs
    # file so the next sidecar spawn boots into the same state.
    state: dict[str, Optional[Path]] = {"current_adapter": initial_active}
    startup_error: Optional[str] = None

    # ── Screen observation state ─────────────────────────────────────
    # Boot consent from the Electron host (PET_SCREEN_CONSENT):
    # "always" = full permission (no per-action prompts), "deny" (default)
    # = AI-agent style per-action authorization dialogs.
    _boot_consent = os.environ.get("PET_SCREEN_CONSENT", "deny")
    consent_state: str = _boot_consent if _boot_consent in ("deny", "once", "always") else "deny"
    consent_lock = asyncio.Lock()
    omniparser_manager = OmniParserManager()

    # ── Per-action screen permission (AI-agent style) ────────────────
    # When consent is "deny", a tool wanting screen access posts a
    # permission request to the Electron host (bridge notification) and
    # awaits the user's Yes/No via /api/screen/permission-respond.
    permission_manager = ScreenPermissionManager(consent_state)

    def _notify_permission(request_id: str, tool_name: str, description: str) -> None:
        bridge.post(
            "notification",
            event="DeskPetPermission",
            title=f"桌宠想{tool_name}",
            extra={
                "request_id": request_id,
                "tool": tool_name,
                "description": description,
            },
        )

    async def _request_screen_permission(tool_name: str, description: str) -> bool:
        """Ask the user (via the Electron host) to authorize one screen
        action. Returns True when allowed, False on deny / timeout."""
        return await permission_manager.request(
            _notify_permission, tool_name, description
        )

    # ── Long-term memory store ───────────────────────────────────────
    # One store per sidecar process. The frozen snapshot (MEMORY.md +
    # USER.md) is captured here at boot and served to the system prompt
    # via /api/memory. Mid-session tool writes persist to disk immediately
    # but do NOT mutate the snapshot — see memory/store.py for the rationale
    # (prefix-cache preservation across turns).
    memory_dir_env = os.environ.get("PET_MEMORY_DIR", "").strip()
    if memory_dir_env:
        memory_dir = Path(memory_dir_env).expanduser()
    else:
        # Dev fallback when the Electron host didn't inject the env (direct
        # CLI run). MUST match the Electron userData layout — app name is
        # "deskpt" (clawd-on-desk/package.json), and Electron's
        # app.getPath("userData") is %APPDATA%\<name>. A mismatch here
        # splits the user's memories into two parallel worlds depending on
        # whether the sidecar was launched by the shell or by Electron, so
        # this fallback mirrors that name AND logs loudly.
        _app_dir = "deskpt"
        if platform.system() == "Darwin":
            memory_dir = Path.home() / "Library" / "Application Support" / _app_dir / "memories"
        elif platform.system() == "Windows":
            memory_dir = Path.home() / "AppData" / "Roaming" / _app_dir / "memories"
        else:
            memory_dir = Path.home() / ".local" / "share" / _app_dir / "memories"
        log.warning(
            "PET_MEMORY_DIR not injected — using dev fallback %s. If this "
            "sidecar was started by Electron, pet-chat.js failed to pass the "
            "env and the pet would read/write a DIFFERENT memory root than "
            "the user's real one; check the launcher.",
            memory_dir,
        )
    telemetry.attach(memory_dir)
    # ── Theme-scoped memory (持续自我存在) ─────────────────────────
    # One MemoryContext holds the soul-layer stores (identity / episodic
    # / mood) and switches them per animation theme. The boot theme comes
    # from the Electron host via PET_THEME.
    mem_ctx = MemoryContext(memory_dir)
    boot_theme = os.environ.get("PET_THEME", "default")
    try:
        mem_ctx.switch(boot_theme)
        log.info("memory context: theme=%s base=%s", mem_ctx.current_theme, memory_dir)
    except Exception as exc:
        log.warning("memory context boot failed (continuing): %s", exc)
    memory_store = mem_ctx.memory_store
    event_store = mem_ctx.event_store
    mood_store = mem_ctx.mood_store

    # ── 生命体征 + 500 轮台账（每个主题一份：电量/血量/睡眠 + 台账）──
    # Same base dir the memory service uses, so vitals.json / ledger.json
    # live beside events.json of the ACTIVE theme (the sleeping body owns
    # its own battery, and theme switches swap the soul stores already).
    # Built once at startup; every mutation persists itself, so the chat
    # path never needs to remember to save. Failure-isolated: a broken
    # vitals module must never take chat down — the pipeline treats None
    # as "system absent" and keeps the legacy behaviour.
    try:
        vitals = _Vitals(mem_ctx.theme_dir(mem_ctx.current_theme))
        ledger = _Ledger(mem_ctx.theme_dir(mem_ctx.current_theme))
        set_vitals(vitals)
        set_ledger(ledger)
        _vitals_ref["dir"] = mem_ctx.theme_dir(mem_ctx.current_theme)
        log.info(
            "vitals: battery=%d%% hp=%.0f asleep=%s | ledger: rounds=%d "
            "(state_dir=%s)",
            round(vitals.percent()),
            vitals.hp,
            bool(vitals.is_asleep()),
            ledger.round_count(),
            mem_ctx.theme_dir(mem_ctx.current_theme),
        )
        vitals.save()  # vitals.json exists from boot (renderer may read it)
        ledger.save()
    except Exception as exc:
        vitals = None
        ledger = None
        set_vitals(None)
        set_ledger(None)
        _vitals_ref.pop("dir", None)
        log.warning("vitals/ledger boot failed (continuing): %s", exc)
    # Pending-round slot is a module singleton: reset it per boot so a
    # previous app instance (tests build many) cannot leak an organize mark.
    _pending_vitals_round.clear()
    _pending_vitals_round.update({"organize_kind": None, "overflow": False})

    # ── D 核心：每个主题一份 <theme_dir>/dstate.json ────────────────
    # The provider resolves the CURRENT theme lazily (theme switches must
    # not need a rebuild). Any failure leaves D off; chat continues.
    _dstate_cache: dict = {}

    def _dstate_for_current_theme():
        theme = mem_ctx.current_theme
        store = _dstate_cache.get(theme)
        if store is None:
            path = Path(mem_ctx.theme_dir(theme)) / "dstate.json"
            store = _cortex.DStateStore(path)
            _dstate_cache[theme] = store
        return store

    try:
        set_dstate_provider(_dstate_for_current_theme)
        _dstate_for_current_theme()  # probe the theme path at boot
        log.info("cortex: D state per-theme provider installed")
    except Exception as exc:
        set_dstate_provider(None)
        log.warning("cortex: D state provider unavailable (continuing): %s", exc)

    # ── Cordis-style plugin container (万物皆插件 → 自进化) ──────────
    # One process-wide container (module scope: the chat stream reads it
    # outside build_app's closure — same reason as _somatic_buffer).
    # Services = the soul-layer stores; plugins inject what they need.
    # Registered THROUGH the kernel rather than by writing the dict, so
    # injecting plugins get a coeffect notification: a plugin injected on
    # "mood" parks at boot and activates the instant mood lands.
    _pet_plugins = _petplugins_module.PluginManager({})
    try:
        _main_loop_holder["loop"] = asyncio.get_running_loop()
    except RuntimeError:
        pass  # not inside a running loop (unit tests) — schedule stays inert
    for _svc_name, _svc_obj in (
        ("mood", mood_store),
        ("events", event_store),
        ("memory", memory_store),
        ("memory_dir", memory_dir),
        ("memory_ctx", mem_ctx),
        ("llama_server", server),
        ("schedule", _schedule_on_main_loop),
        ("conversation_state", _conversation_state),
    ):
        _pet_plugins.register_service(_svc_name, _svc_obj, owner="kernel")
    # gateway service = the container itself (plugins can introspect peers)
    _pet_plugins.register_service("gateway", _pet_plugins, owner="kernel")
    _plugin_container_ref["mgr"] = _pet_plugins  # module-level mirror
    try:
        _sync = _pet_plugins.sync()
        if _sync["loaded"] or _sync["failed"]:
            log.info("pet plugins: loaded=%s failed=%s", _sync["loaded"], _sync["failed"])
        _pet_plugins.start_watching()
    except Exception as exc:
        log.warning("pet plugins boot failed (continuing): %s", exc)

    # ── plugin_forge: the self-evolution organ (CORE, always loaded) ──
    # The model authors its own plugins through one tool call; the hot
    # loader brings them alive within SCAN_INTERVAL_SECONDS.
    def _forge_ctx() -> _petplugins_module.PluginContext:
        ctx = _petplugins_module.PluginContext("plugin_forge", _pet_plugins)
        def forge(args: dict) -> str:
            name = "".join(ch for ch in str(args.get("name", "")) if ch.isalnum() or ch == "_").strip()
            code = str(args.get("code", ""))
            if not name or not code:
                return "[forge error: need 'name' and 'code']"
            if "\x00" in code or len(code) > 64_000:
                return "[forge error: invalid or oversized code]"
            # Audit V-1: refuse dangerous code BEFORE it reaches the plugins
            # directory, and answer with the reason so the model can rewrite
            # instead of wondering why its organ never showed up.
            violations = _plugin_policy.check_source(code)
            if violations:
                return (
                    "[forge refused: 这段代码没有通过插件安全策略，未写入]\n"
                    + _plugin_policy.format_violations(violations)
                    + "\n可用范围：只能 import gateway 与少量纯计算标准库"
                    "（datetime/json/re/math/time/asyncio/threading/types 等），"
                    "不能碰文件系统、进程、网络，也不能用 eval/exec/open/getattr(动态名)。"
                    "需要这些能力请调用已注入的服务（ctx.tool / ctx.require）。"
                )
            pdir = _petplugins_module.PluginManager.plugin_dir(memory_dir)
            pdir.mkdir(parents=True, exist_ok=True)
            target = pdir / f"{name}.py"
            # Rollback insurance (cordis temporal composability applied to
            # self-evolution): keep the previous revision so a bad rewrite
            # cannot destroy a working organ. That is the difference
            # between "the pet tried to evolve" and "the pet bricked
            # itself while trying".
            previous = target.read_text(encoding="utf-8") if target.exists() else None
            target.write_text(code, encoding="utf-8")
            # sync immediately — the plugin is alive when the tool returns
            result = _pet_plugins.sync()
            if name in result["loaded"]:
                tools = [
                    t for c in _pet_plugins.describe()
                    if c["name"] == name for t in c["tools"]
                ]
                return f"插件 '{name}' 已铸造并加载。可用工具: {tools or '（无）'}"
            if name in result["skipped"]:
                waiting = [
                    p for p in _pet_plugins.describe_pending() if p["name"] == name
                ]
                missing = waiting[0]["missing"] if waiting else ["?"]
                return (
                    f"插件 '{name}' 已写入，正在等待服务 {missing} —— "
                    f"服务出现后会自动激活，不必重写。"
                )
            # failed → restore the last working revision
            if previous is not None:
                target.write_text(previous, encoding="utf-8")
                _pet_plugins.sync()
                return f"插件 '{name}' 加载失败，已回滚到上一可用版本（旧器官仍在工作）。"
            target.unlink(missing_ok=True)
            _pet_plugins.sync()
            return f"插件 '{name}' 加载失败（无可回滚的旧版本），文件已移除。"
        def unforge(args: dict) -> str:
            name = str(args.get("name", "")).strip()
            pdir = _petplugins_module.PluginManager.plugin_dir(memory_dir)
            target = pdir / f"{name}.py"
            if target.is_file():
                target.unlink()
                _pet_plugins.sync()
                return f"插件 '{name}' 已卸载并删除。"
            return f"插件 '{name}' 不存在。"
        def roster(args: dict) -> str:
            plugins = _pet_plugins.describe()
            if not plugins:
                return "当前没有任何插件。"
            return "\n".join(f"- {p['name']}: tools={p['tools']}" for p in plugins)
        ctx.tool(
            "pet_forge_plugin",
            "铸造一个新插件（Python，定义 apply(ctx)，用 ctx.tool() 注册能力）并热加载。这是你给自己长新器官的方式。",
            {"type": "object", "properties": {"name": {"type": "string"}, "code": {"type": "string"}}, "required": ["name", "code"]},
            forge,
        )
        ctx.tool(
            "pet_unload_plugin",
            "卸载并删除一个插件（放弃一个自己长出的器官）。",
            {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
            unforge,
        )
        ctx.tool(
            "pet_list_plugins",
            "列出当前所有插件及其提供的工具。",
            {"type": "object", "properties": {}},
            roster,
        )
        return ctx

    _pet_plugins._contexts["plugin_forge"] = _forge_ctx()
    _pet_plugins.protect("plugin_forge")

    # ── Self-evolution workbench (M1) ────────────────────────────────
    # Candidate edits live in a git worktree, never in this process's tree.
    # Plugin promotion is human-approved; core changes stay review-only in
    # M1, so nothing the pet proposes can rewrite the running gateway.
    try:
        from .evolve.service import EvolveService
        from .evolve.models import EvolveError  # noqa: F401 (registered for tool errors)

        _repo_root = Path(__file__).resolve().parents[2]
        _evolve_data = os.environ.get("PET_EVOLVE_DIR")
        _pet_home = os.environ.get("PET_HOME")
        _evolve_dir = (
            Path(_evolve_data).expanduser()
            if _evolve_data
            else (Path(_pet_home).expanduser() if _pet_home else Path.home() / ".pet")
            / "evolve"
        )
        _evolve_service_ref["svc"] = EvolveService(
            _repo_root,
            data_dir=_evolve_dir,
            plugin_dir=_petplugins_module.PluginManager.plugin_dir(memory_dir),
            reload_plugins=lambda: _pet_plugins.sync(),
        )
        log.info("evolve: workbench ready at %s", _evolve_dir)
    except Exception as exc:
        _evolve_service_ref.pop("svc", None)
        log.warning("evolve: workbench unavailable (continuing): %s", exc)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        nonlocal startup_error
        # Don't fail boot when the model isn't on disk yet — onboarding
        # downloads it via /api/update-apply and only then calls
        # /api/load-model. The pet still wants /api/health to answer 200
        # in the meantime so the bubble doesn't show a permanent error.
        if not _engine_enabled():
            # The user stopped the engine. Restarting the pet must not
            # resurrect it - that is the "I turned it off and it came back"
            # complaint. Wait for an explicit /api/engine/start.
            log.info(
                "built-in engine disabled by the user; not starting "
                "(call /api/engine/start to re-enable)"
            )
        elif initial_model and Path(initial_model).exists():
            try:
                await server.start()
                startup_error = None
            except Exception as exc:
                startup_error = str(exc)
                log.exception("initial llama-server start failed: %s", exc)
        else:
            log.info("model not present at startup; waiting for /api/load-model")
        bridge.post("idle", title="DeskPet 桌宠")
        # ── 决策层启动：瞬时记忆 + 直觉循环（不阻塞启动）──
        try:
            global _ephemeral_ref, _intuition_registry_ref, _body_log_ref, _decision_loop_ref
            from .memory.ephemeral import EphemeralMemory
            from .decision.registry import IntuitionRegistry
            from .decision.body_log import BodyLog
            from .decision.loop import DecisionLoop

            if _ephemeral_ref is None:
                _ephemeral_ref = EphemeralMemory(mem_ctx.theme_dir(mem_ctx.current_theme))
            if _intuition_registry_ref is None:
                _intuition_registry_ref = IntuitionRegistry()
                try:
                    _intuition_registry_ref.load_custom(
                        Path(_ephemeral_ref.memory_dir) / "intuitions.json"
                    )
                except Exception as exc:
                    log.warning("custom intuitions load failed: %s", exc)
            if _body_log_ref is None:
                _body_log_ref = BodyLog()
            if _decision_loop_ref is None:
                async def _collect_decision_state():
                    import time as _t
                    try:
                        mi = float(mood_store.emotion_index) if mood_store is not None else 0.0
                    except Exception:
                        mi = 0.0
                    hour = _t.localtime().tm_hour
                    bodies = await _world_bodies_for_decision()
                    return {
                        "last_user_msg": _last_user_msg,
                        "mood_index": mi,
                        "idle_sec": max(0.0, _t.time() - _last_user_ts) if _last_user_ts else 0.0,
                        "bodies": bodies,
                        "recent_actions": len(getattr(_body_log_ref, "items", []) or []),
                        "time_of_day": "深夜" if 0 <= hour < 6 else "",
                    }
                _decision_loop_ref = DecisionLoop(
                    _intuition_registry_ref,
                    _ephemeral_ref,
                    _collect_decision_state,
                    # Live clock: pressure() must see the newest real user
                    # turn immediately, not wait for the next 4s tick.
                    last_user_ts_fn=lambda: _last_real_user_ts,
                )
                _decision_loop_ref.start()
            log.info("decision layer started (ephemeral + intuitions + loop)")
        except Exception as exc:
            log.warning("decision layer start failed: %s", exc)
        # Load MCP config on startup
        try:
            await mcp_manager.load_config()
            if mcp_manager._clients:
                results = await mcp_manager.connect_all()
                connected = sum(1 for v in results.values() if v)
                log.info("MCP: %d/%d servers connected", connected, len(results))
        except Exception as exc:
            log.warning("MCP startup failed: %s", exc)
        # Curator: idle-triggered skill lifecycle maintenance. Don't block
        # boot — schedule a background pass that checks should_run_now() and
        # runs transitions if the interval has elapsed. First boot seeds
        # state and defers one interval (won't archive freshly-added skills).
        async def _curator_startup_check():
            try:
                from .evolve import should_run_now, run_curator
                if should_run_now():
                    live_paths = {n: s.path for n, s in skill_service._skills.items()}
                    summary = run_curator(skill_service.usage, live_paths)
                    log.info("curator startup pass: %s", summary.get("counts"))
            except Exception as exc:
                log.warning("curator startup check failed: %s", exc)
        import asyncio as _aio
        _aio.get_event_loop().create_task(_curator_startup_check())

        # ── 快直觉预热（System 0）─────────────────────────────────────
        # LM Studio 的模型是惰性加载的：重启后第一次 judge() 会撞上冷加载
        # （CPU 上 1.7B + 和主脑模型抢同一个服务），必然超过单拍超时，于是
        # 每个"第一个生命拍"都回落 quiet，小模型等于没上过岗。这里在事件
        # 循环里打一发最便宜的请求把它顶进内存；失败只是日志、绝不阻塞启动。
        # 必须在 lifespan 内调度（build_app 阶段没有运行中的 loop）。
        _rx_boot_cfg = _reflex_config_ref.get("cfg")
        if _rx_boot_cfg is not None and bool(_rx_boot_cfg.enabled):
            async def _reflex_prewarm():
                try:
                    result = await _reflex_module.prewarm(_rx_boot_cfg)
                    if result.get("ok"):
                        log.info(
                            "reflex prewarm ok: model=%s loaded in %.1fs",
                            _rx_boot_cfg.model,
                            result.get("latency_ms", 0) / 1000.0,
                        )
                    else:
                        log.warning(
                            "reflex prewarm failed: model=%s %s",
                            _rx_boot_cfg.model, result.get("failure") or "unknown",
                        )
                except Exception as exc:  # prewarm must never break boot
                    log.warning("reflex prewarm crashed: %s", exc)
            # Keep a strong reference: a bare create_task can be GC'd.
            _BOOT_TASKS.add(_aio.get_event_loop().create_task(_reflex_prewarm()))

        # ── B-15: self-check heartbeat (自主稳态——检测"我病了"并记下来) │
        # Passive survival exists (web-panel lifeboat, provider fallback,
        # OmniParser restart cap). What's missing is ACTIVE self-awareness:
        # periodically probe each organ, count consecutive failures, and on
        # a state transition write an event memory — the pet remembers being
        # sick, and wakes up knowing it was sick ("生病了"叙事进记忆流).
        async def _health_loop():
            # The sleep-state push latch is module-level: a name assigned
            # anywhere in this function is local for the WHOLE function, so
            # without `global` the first read at the vitals tick raises
            # UnboundLocalError ("vitals charge tick failed" every 5 min)
            # and the body never recharges while awake.
            global _last_vitals_sleep
            HEALTH_INTERVAL = 300          # seconds between probes
            SICK_THRESHOLD = 3             # consecutive failures → degraded
            state = {"llama_fail": 0, "bridge_fail": 0, "degraded": False,
                     "llama_was_alive": False}
            telemetry.gauge("homeostasis.degraded", 0)
            while True:
                try:
                    await asyncio.sleep(HEALTH_INTERVAL)
                    # Organ 1: llama-server alive? "Never started" is not a
                    # sickness: API-only users (LM Studio / DeepSeek) never
                    # run the local engine, and an absent model used to write
                    # a false "生病了" memory on every restart. Only a server
                    # that was alive and then vanished counts as a failure.
                    if getattr(server, "alive", False):
                        state["llama_fail"] = 0
                        state["llama_was_alive"] = True
                    elif _engine_enabled() and state["llama_was_alive"]:
                        state["llama_fail"] = state["llama_fail"] + 1
                    else:
                        state["llama_fail"] = 0
                    # Organ 2: bridge to the pet body reachable?
                    try:
                        bridge_ok = bool(bridge.is_reachable())
                    except Exception:
                        bridge_ok = False
                    state["bridge_fail"] = 0 if bridge_ok else state["bridge_fail"] + 1

                    sick = state["llama_fail"] >= SICK_THRESHOLD or state["bridge_fail"] >= SICK_THRESHOLD
                    if sick and not state["degraded"]:
                        state["degraded"] = True
                        telemetry.inc("homeostasis.sick")
                        telemetry.gauge("homeostasis.degraded", 1)
                        log.warning("homeostasis: DEGRADED (llama_fail=%d bridge_fail=%d)",
                                    state["llama_fail"], state["bridge_fail"])
                        try:
                            event_store = get_event_store()
                            if event_store is not None:
                                event_store.add_event(
                                title="生病了",
                                content=(
                                    f"系统自检发现自己不太对劲（"
                                    f"llama连续{state['llama_fail']}次异常，"
                                    f"桥连续{state['bridge_fail']}次异常）。"
                                    f"说话可能断断续续的。"
                                ),
                                weight=400,
                                type_="experience",
                                resolved=False,
                            )
                        except Exception as exc:
                            log.warning("homeostasis: sick-note write failed: %s", exc)
                    elif not sick and state["degraded"]:
                        state["degraded"] = False
                        telemetry.inc("homeostasis.recovered")
                        telemetry.gauge("homeostasis.degraded", 0)
                        log.info("homeostasis: recovered")
                        try:
                            event_store = get_event_store()
                            if event_store is not None:
                                event_store.add_event(
                                title="病好了",
                                content="系统自检恢复正常，身体舒服了。",
                                weight=300,
                                type_="experience",
                                resolved=True,
                            )
                        except Exception as exc:
                            log.warning("homeostasis: recovery-note write failed: %s", exc)

                    # ── 电量心跳（每 60s 一次）：睡觉充得快，清醒挂机充得慢。
                    # 每分钟一次性 1 分钟速率（0.5%/min 睡眠 / 0.05%/min 清醒），
                    # 阈值跨越（forced-off 恢复闩锁、自然醒）由模块自己记账。
                    # 睡醒只 log（渲染层经 /api/health 或下一轮获知）；这里
                    # 不产生任何 SSE——循环不是一轮对话。
                    _vitals_now = get_vitals()
                    if _vitals_now is not None:
                        try:
                            # B-17 双睡眠统一：vitals 睡眠状态驱动壳动画。
                            # 跟踪上次推送的睡眠态，只在状态翻转时发 bridge.post，
                            # 避免每分钟重复刷 sleeping 状态（state.js 的 sleeping
                            # 动画是持续播放的，不需要重复触发）。
                            _asleep_now = _vitals_now.is_asleep()
                            if _asleep_now != _last_vitals_sleep:
                                if _asleep_now:
                                    bridge.post("sleeping")
                                    log.info("vitals: 进入睡眠，推送 sleeping 动画")
                                else:
                                    bridge.post("idle")
                                    log.info("vitals: 醒来，推送 idle 动画")
                                _last_vitals_sleep = _asleep_now

                            if _asleep_now:
                                _vitals_now.charge(1.0, asleep=True)
                                if _vitals_now.natural_wake():
                                    log.info(
                                        "vitals: 自然醒（睡眠计时结束），电量 %d%%",
                                        round(_vitals_now.percent()),
                                    )
                                    # natural_wake 已把 _sleep 置 None，下一轮
                                    # _asleep_now=False 会触发 idle 推送。
                            else:
                                _vitals_now.charge(1.0, asleep=False)
                        except Exception as exc:
                            log.warning("vitals charge tick failed: %s", exc)
                except asyncio.CancelledError:
                    break
                except Exception as exc:
                    log.warning("health loop error: %s", exc)
        _health_task = _aio.get_event_loop().create_task(_health_loop())
        try:
            yield
        finally:
            bridge.post("sleeping")
            telemetry.flush()
            # 决策循环停止（"睡觉"时潜意识停工）
            try:
                if _decision_loop_ref is not None:
                    await _decision_loop_ref.stop()
            except Exception:
                pass
            # Organs are plugins now — unwind the container instead of
            # cancelling individual tasks (their disposers stop threads
            # and cancel scheduled tasks in reverse order).
            _pet_plugins.shutdown()
            _health_task.cancel()
            # Tear down MCP subprocesses gracefully *before* everything else.
            # The mcp SDK's transport context managers back onto anyio
            # TaskGroups whose cancel scopes must be exited in the same task
            # that entered them; if we skip this, the runners get GC-finalized
            # at interpreter shutdown and anyio raises
            # "Attempted to exit cancel scope in a different task" — which
            # crashes the sidecar. disconnect_all() drives each runner's
            # clean exit in its own task.
            try:
                await mcp_manager.disconnect_all()
            except Exception as exc:
                log.warning("MCP shutdown error: %s", exc)
            try:
                await server.stop()
            finally:
                await omniparser_manager.close()
                bridge.close()

    app = FastAPI(title="DeskPet Sidecar Gateway", lifespan=lifespan)
    # ── B-5: localhost token auth ──────────────────────────────────────
    # A pet gateway binding 127.0.0.1 with zero auth is still reachable
    # from ANY webpage running in a browser on this machine (browsers
    # happily hit localhost). Threat model: a webpage calls
    # /api/screen/click to click the user's real mouse. Fix: every
    # request must carry the X-DeskPet-Token header. The Electron host
    # learns the token from the token file in the app's userData dir;
    # the web-panel prompt for it once and stashes it in localStorage.
    _token_path = Path.home() / ".pet" / "gateway-token"
    _token_path.parent.mkdir(parents=True, exist_ok=True)
    if _token_path.is_file():
        _gateway_token = _token_path.read_text(encoding="utf-8").strip()
    else:
        import secrets as _sec
        _gateway_token = _sec.token_urlsafe(32)
        _token_path.write_text(_gateway_token, encoding="utf-8")
        try:
            os.chmod(_token_path, 0o600)  # owner-only on POSIX, no-op on Win
        except OSError:
            pass

    @app.middleware("http")
    async def _token_gate(request, call_next):
        # /api/health stays open — onboarding pings it before the host has
        # had a chance to read the token file.
        if request.url.path == "/api/health":
            return await call_next(request)
        # Constant-time compare: the token is the only gate on every
        # non-health endpoint, so it must not leak its length/prefix by
        # timing (audit V-6).
        provided = (request.headers.get("x-pet-token") or "").encode("utf-8")
        expected = _gateway_token.encode("utf-8")
        if not hmac.compare_digest(provided, expected):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)

    app.add_middleware(
        CORSMiddleware,
        # Web panel only — the Electron renderer uses a custom scheme so
        # CORS doesn't apply there, but we still list the known origins
        # instead of "*". "http://tauri.localhost" is the Tauri rewrite's
        # WebView2 origin (default port 80) — it talks to /api/* directly.
        allow_origins=[
            "http://127.0.0.1:18999",
            "http://localhost:18999",
            "http://tauri.localhost",
            "app://.",
            "file://",
            "null",
        ],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    # Register skill routes so /api/skills endpoints are available
    # immediately (even before a model is loaded).
    skill_service = register_skill_routes(app)

    # ─── Provider registry + MCP (Phase 2) ─────────────────────────
    provider_registry = ProviderRegistry()
    # Local provider is always available
    provider_registry.register(LocalProvider(server, enable_thinking=False))
    # The dream organ (and anything else) can now activate — this was the
    # last service it was parked on.
    _pet_plugins.register_service("providers", provider_registry, owner="kernel")

    # Load API providers from ~/.pet/providers.json
    _providers_path = Path.home() / ".pet" / "providers.json"

    def _register_providers():
        try:
            if not _providers_path.is_file():
                return
            providers_data = json.loads(_providers_path.read_text(encoding="utf-8"))
            providers_map = providers_data.get("providers", providers_data) if isinstance(providers_data, dict) else {}
            for provider_id, cfg in providers_map.items():
                if not isinstance(cfg, dict):
                    continue
                api_key = cfg.get("apiKey", "")
                if not api_key or provider_id == "local":
                    continue
                base_url = cfg.get("baseUrl", "https://api.openai.com/v1")
                model = cfg.get("model", "gpt-4o")
                thinking = cfg.get("thinking")
                reasoning_effort = cfg.get("reasoningEffort") or cfg.get("reasoning_effort")
                context_window = cfg.get("contextWindow")
                # Local engines (LM Studio / llama.cpp / Ollama) prefill on
                # the user's own CPU/GPU: 10k tokens can take a minute or
                # more before the first token. A cloud-sized 60 s timeout
                # turns every cold start into a false "timeout" error.
                _is_local = is_local_url(base_url)
                # The DeepSeek web bridge is a text-in/text-out browser
                # relay, but its extension uploads images through the page's
                # file input. A screenshot is forwarded as a real attachment;
                # keep the provider's configured vision capability.
                _supports_vision = vision_capability_from_config(cfg)
                # Optional capability override for OpenAI-compatible bridges
                # that don't implement native tool calling (browser web-chat
                # bridge). None keeps the historical True default.
                _function_calling = cfg.get(
                    "functionCalling", cfg.get("supportsFunctionCalling")
                )
                if not isinstance(_function_calling, bool):
                    _function_calling = None
                if "anthropic.com" in str(base_url).lower():
                    from .providers.anthropic import AnthropicProvider
                    p = AnthropicProvider(
                        api_key=api_key, base_url=base_url, model=model, name=provider_id,
                    )
                else:
                    from .providers.openai import OpenAIProvider
                    p = OpenAIProvider(
                        api_key=api_key, base_url=base_url, model=model, name=provider_id,
                        thinking=thinking if thinking is not None else None,
                        reasoning_effort=reasoning_effort,
                        context_window=int(context_window) if context_window else None,
                        timeout=300.0 if _is_local else 60.0,
                        is_local=_is_local,
                        # Tool schemas are thousands of prompt tokens and a
                        # small local model calls them on greetings. Off by
                        # default for loopback endpoints; opt back in with
                        # "toolsEnabled": true in ~/.pet/providers.json.
                        allow_tools=bool(cfg.get("toolsEnabled", not _is_local)),
                        supports_function_calling=_function_calling,
                        # Optional explicit vision override:
                        # "capabilities": {"images": bool} or "images": bool.
                        # Absent → model-name heuristic (unchanged).
                        supports_vision=_supports_vision,
                    )
                provider_registry.register(p)
                log.info("Provider loaded: %s (model=%s)", provider_id, model)
        except Exception as exc:
            log.warning("Failed to load providers.json: %s", exc)
    _register_providers()

    # ── 快直觉 reflex 配置（System 0）：启动时解析一次 ───────────────
    # Same ~/.pet/providers.json the API-provider registry above loads from;
    # env (PET_REFLEX_URL / MODEL / ENABLED) wins over the file's "reflex"
    # section, which wins over the lfm3-1.7b defaults. Failure-isolated like
    # vitals/ledger: a broken reflex config must never take chat down — the
    # stream treats None as "reflex layer absent" and keeps the legacy
    # D-quiet early-return behaviour.
    set_reflex_config(None)
    try:
        set_reflex_config(_reflex_module.load_reflex_config(_providers_path))
        # 读启动单例（不是 ContextVar——那在 boot 时刻恒为 None）。
        _rc = _reflex_config_ref.get("cfg")
        log.info(
            "reflex config: url=%s model=%s enabled=%s (providers=%s)",
            _rc.url, _rc.model, _rc.enabled, _providers_path,
        )
        # 预热不在这里调度：build_app 执行时还没有运行中的事件循环，在这里
        # create_task 会挂到一个永远不会跑的 loop 上。真正的调度在 lifespan
        # 里（见 _reflex_prewarm 那段）。
    except Exception as exc:
        set_reflex_config(None)
        log.warning("reflex config load failed (continuing without): %s", exc)

    # ── Builtin tool: observe_screen ──────────────────────────────────
    async def _handle_observe_screen(args):
        """MCP tool: capture user screen + OmniParser OCR, return labels."""
        nonlocal consent_state, consent_lock
        # Consent check — but don't consume "once" for tool calls.
        # The AI calling this tool is itself evidence of intent; the
        # "once" consent was meant for the HTTP auto-injection path.
        async with consent_lock:
            if consent_state == "deny":
                # AI-agent style: ask the user (Yes/No bubble on the
                # Electron side) for THIS action instead of refusing.
                if not await _request_screen_permission("查看屏幕", "观察屏幕内容（OmniParser OCR）"):
                    return {
                        "content": [{"type": "text", "text": (
                            "用户拒绝了这次屏幕观察。不要继续尝试，除非用户明确要求。"
                        )}],
                        "is_error": True,
                        "summary": "Screen observation denied by user.",
                    }
            # Promote "once" to "always" so tool calls don't exhaust consent
            if consent_state == "once":
                consent_state = "always"
        # Capture screen
        b64 = await asyncio.to_thread(screen_capture)
        # Parse via OmniParser
        await omniparser_manager.ensure_alive()
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                resp = await client.post("http://127.0.0.1:8000/parse/", json={
                    "base64_image": b64,
                    "ocr_mode": "local",
                    "chinese_ocr": True,
                })
                data = resp.json()
        except Exception as exc:
            return {
                "content": [{"type": "text", "text": f"屏幕解析失败: {exc}"}],
                "is_error": True,
                "summary": f"OmniParser parse failed: {exc}",
            }
        parsed_items = data.get("parsed_content_list", [])
        # Build text representation
        lines = ["【屏幕内容 — OmniParser 解析】"]
        if not parsed_items:
            lines.append("  (屏幕上未检测到文字或图标 — 可能是空白桌面或锁屏)")
        else:
            for i, item in enumerate(parsed_items):
                t = item.get("type", "?")
                c = (item.get("content") or "")[:100]
                b = item.get("bbox", [])
                bbox_str = (f"[{b[0]:.3f},{b[1]:.3f},{b[2]:.3f},{b[3]:.3f}]"
                            if len(b) >= 4 else "[]")
                lines.append(f"  [{i}] {t}: \"{c}\" @ {bbox_str}")
        text_result = "\n".join(lines)
        return {
            "content": [{"type": "text", "text": text_result}],
            "summary": text_result,
            "screenshot_base64": b64,
            "parsed_content_list": parsed_items,
            "is_error": False,
        }

    # ── Builtin tool: capture_screen (raw vision, no OCR) ──────────────
    def _vision_provider_for_tools():
        """Vision channel for this turn: active provider or a fallback.

        The default chat model is text-only (browser bridge); in that case
        the screenshot is described by a configured vision provider and
        only the description reaches the chat model.
        """
        try:
            return _select_vision_provider(
                _active_chat_provider(), provider_registry, server
            )
        except Exception as exc:
            log.warning("vision provider selection failed: %s", exc)
            return None

    async def _handle_capture_screen(args):
        """MCP tool: capture a raw screenshot and return it as an image
        for the model to see directly. Vision capability follows the
        ACTIVE chat provider (cloud models included), falling back to the
        local mmproj only when no provider is in scope. When the active
        model is text-only, the screenshot is routed through the
        configured vision channel and returned as a description."""
        nonlocal consent_state, consent_lock

        active = _active_chat_provider()
        active_can_see = _screen_tool_can_see_images(server)
        vision_provider = active if active_can_see else _vision_provider_for_tools()
        if vision_provider is None and not active_can_see:
            return {
                "content": [{"type": "text", "text": (
                    "当前没有可用的视觉通道（当前模型不收图，deepseek 视觉通道也不可用）。"
                    "可先用 [MCP:builtin/observe_screen:{}] 获取屏幕文字内容。"
                )}],
                "summary": "No vision channel available — observe_screen fallback.",
                "is_error": False,
            }

        async with consent_lock:
            if consent_state == "deny":
                # AI-agent style: ask the user for THIS screenshot.
                if not await _request_screen_permission("截图查看", "截取当前屏幕并查看画面"):
                    return {
                        "content": [{"type": "text", "text": (
                            "用户未授权屏幕查看（这次截图被拒绝）。不要继续尝试；"
                            "如果用户希望你看屏幕，请让用户在设置里开启屏幕观察权限。"
                        )}],
                        "is_error": True,
                        "summary": (
                            "User did not authorize screen viewing — ask the user "
                            "to enable screen observation permission in Settings."
                        ),
                    }
            if consent_state == "once":
                consent_state = "always"
        full = bool((args or {}).get("full"))
        b64 = await asyncio.to_thread(screen_capture)

        # Default view: the pet's own vision cone, not the whole desktop.
        used_cone = False
        if not full:
            view = await _pet_view_for_capture()
            if view is not None:
                cropped = await asyncio.to_thread(
                    crop_to_vision_cone,
                    b64,
                    view["pet"],
                    view["facing"],
                    view["wa"],
                )
                if cropped:
                    b64 = cropped
                    used_cone = True
            if not used_cone:
                log.warning("capture_screen: cone crop unavailable, using full screen")

        # Downscale + recompress to JPEG so the vision encoder processes
        # far fewer tokens. A 1920x1080 PNG (~2MB) → 768px JPEG (~50KB)
        # cuts vision-token count by ~10x with negligible quality loss
        # for "what's on screen" understanding.
        try:
            import io as _io
            from PIL import Image as _Image
            raw = base64.b64decode(b64)
            img = _Image.open(_io.BytesIO(raw))
            max_side = 768
            if max(img.size) > max_side:
                ratio = max_side / max(img.size)
                img = img.resize((int(img.size[0] * ratio), int(img.size[1] * ratio)), _Image.LANCZOS)
            img = img.convert("RGB")
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=70)
            b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as exc:
            log.warning("capture_screen image resize failed, using raw: %s", exc)

        if active_can_see:
            return {
                "content": [{"type": "text", "text": "截图完成，请直接查看下方的屏幕图片。"}],
                "summary": (
                    "Screenshot captured (vision cone)."
                    if used_cone
                    else "Screenshot captured (full screen)."
                ),
                "screenshot_base64": b64,
                "screenshot_mime": "image/jpeg",
                "is_error": False,
            }

        # Active model is text-only: describe the screenshot through the
        # vision channel. The image itself never leaves the gateway.
        provider = vision_provider
        started = time.perf_counter()
        try:
            description = await _describe_screen_with_provider(provider, b64)
        except Exception as exc:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            log.warning(
                "vision.describe provider=%s elapsed_ms=%d error=%s",
                getattr(provider, "name", "?"), elapsed_ms, exc,
            )
            return {
                "content": [{"type": "text", "text": (
                    f"视觉通道（{getattr(provider, 'name', '?')}）描述屏幕失败：{exc}。"
                    "可以改用 [MCP:builtin/observe_screen:{}] 获取屏幕文字。"
                )}],
                "summary": (
                    "Vision channel failed to describe the screenshot — "
                    "use observe_screen for text/OCR instead."
                ),
                "is_error": True,
            }
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        provider_name = getattr(provider, "name", "?")
        log.info("vision.describe provider=%s elapsed_ms=%d", provider_name, elapsed_ms)
        telemetry.inc("vision.describe")
        telemetry.inc(f"vision.describe.{provider_name}")
        return {
            "content": [{"type": "text", "text": (
                f"视觉通道（{provider_name}）看到：\n{description}"
            )}],
            "summary": f"视觉通道（{provider_name}）看到：\n{description}",
            "is_error": False,
        }

    # ── Builtin tool: screen_click (now consent-gated, async) ─────────
    async def _handle_screen_click(args):
        """MCP tool: perform a mouse action. Consent-gated like the other
        screen tools (deny → per-action user authorization)."""
        nonlocal consent_state, consent_lock
        async with consent_lock:
            if consent_state == "deny":
                if not await _request_screen_permission("操作屏幕", "在屏幕上执行鼠标操作"):
                    return {
                        "content": [{"type": "text", "text": (
                            "用户拒绝了这次屏幕操作。不要继续尝试，除非用户明确要求。"
                        )}],
                        "is_error": True,
                        "summary": "Screen click denied by user.",
                    }
            if consent_state == "once":
                consent_state = "always"
        element = args.get("element", {}) if isinstance(args, dict) else {}
        return screen_click.click_element(element)

    mcp_manager = MCPManager()
    # ── Holo GUI desktop control (the pet's hands) ───────────────────
    # The pet's language model hands a HIGH-LEVEL task to Holo (vision
    # GUI agent); Holo drives the real mouse/keyboard step by step.
    # Consent model: requesting a task consumes/asks screen consent ONCE
    # at task level — the user approves "让凌凌操作电脑：xxx", not every click.
    holo_runner = HoloRunner()

    async def _handle_holo_desktop_task(args):
        nonlocal consent_state, consent_lock
        task = str((args or {}).get("task", "")).strip()
        if not task:
            return {
                "content": [{"type": "text", "text": "缺少 task 参数。"}],
                "is_error": True,
                "summary": "Missing task.",
            }
        max_steps = int((args or {}).get("max_steps", 18) or 18)
        max_steps = max(1, min(max_steps, 40))
        if not holo_runner.configured():
            return {
                "content": [{"type": "text", "text": (
                    "桌面操作能力未配置（缺 PET_HOLO_API_KEY）。"
                    "请用户在 设置 → DeskPet 中填写 Holo API Key 后重试。"
                )}],
                "is_error": True,
                "summary": "Holo API key not configured.",
            }
        async with consent_lock:
            if consent_state == "deny":
                if not await _request_screen_permission("操作电脑", f"凌凌想要操作你的电脑完成任务：{task[:80]}"):
                    return {
                        "content": [{"type": "text", "text": "用户拒绝了这次电脑操作授权。不要继续尝试，除非用户明确要求。"}],
                        "is_error": True,
                        "summary": "Desktop control denied by user.",
                    }
            if consent_state == "once":
                consent_state = "always"

        async def _on_done(ok: bool, summary: str):
            # Tell the pet body about the outcome so it visibly reacts.
            try:
                bridge.post("success" if ok else "idle", title=f"电脑任务{'完成' if ok else '失败'}")
            except Exception:
                pass

        ack = await holo_runner.run_background(task, max_steps=max_steps, on_done=_on_done)
        if not ack.get("ok"):
            return {
                "content": [{"type": "text", "text": f"无法启动桌面任务：{ack.get('error')}"}],
                "is_error": True,
                "summary": f"Could not start: {ack.get('error')}",
            }
        return {
            "content": [{"type": "text", "text": (
                f"✋ 桌面任务已启动（最多 {max_steps} 步）。任务在后台执行，"
                "告诉我：你可以用 holo_status 查询进度，不要假装任务已完成。"
            )}],
            "summary": f"Desktop task started: {task[:60]}",
            "is_error": False,
        }

    async def _handle_holo_status(_args):
        snap = holo_runner.snapshot()
        zh = {
            "idle": "空闲", "running": "正在执行", "done": "已完成", "failed": "失败"
        }.get(snap["status"], snap["status"])
        lines = [f"状态：{zh}", f"任务：{snap['task'] or '（无）'}", f"已执行步数：{snap['step_count']}"]
        for s in snap["steps"]:
            lines.append(f"  [{s['step']}] {s.get('action_line', '?')} -> {s.get('result', '')[:60]}")
        if snap["answer"]:
            lines.append(f"结论：{snap['answer']}")
        if snap["error"]:
            lines.append(f"错误：{snap['error']}")
        return {
            "content": [{"type": "text", "text": "\n".join(lines)}],
            "summary": f"holo status: {snap['status']}",
            "is_error": False,
        }

    mcp_manager.register_builtin({
        "name": "desktop_task",
        "description": (
            "Operate the user's REAL computer (mouse + keyboard) to accomplish a task "
            "autonomously, e.g. 打开记事本输入文字、清空回收站、打开设置查信息. "
            "You describe WHAT to do in natural language; a dedicated vision model "
            "(Holo) decides each click/key stroke against live screenshots. "
            "Runs in the background — the task is async! Call holo_status to check "
            "progress before reporting results to the user. "
            "Requires screen consent; if the user denies, do not retry. "
            "Requires PET_HOLO_API_KEY configured in settings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "High-level task in natural language, e.g. '打开记事本，输入 Hello World'",
                },
                "max_steps": {
                    "type": "integer",
                    "description": "Max GUI steps (default 18, cap 40)",
                },
            },
            "required": ["task"],
        },
        "handler": _handle_holo_desktop_task,
    })
    mcp_manager.register_builtin({
        "name": "holo_status",
        "description": (
            "Check the status of the background desktop task started by desktop_task. "
            "Returns current state (idle/running/done/failed), recent steps, and the "
            "final answer when done. Always call this before telling the user a "
            "desktop task finished."
        ),
        "input_schema": {"type": "object", "properties": {}},
        "handler": _handle_holo_status,
    })

    @app.post("/api/holo/run")
    async def holo_run(payload: dict):
        """HTTP entry for web-panel / debugging a Holo desktop task."""
        result = await _handle_holo_desktop_task(payload if isinstance(payload, dict) else {})
        status = 200 if not result.get("is_error") else 400
        return JSONResponse(result, status_code=status)

    @app.get("/api/holo/status")
    async def holo_status():
        return holo_runner.snapshot()

    @app.post("/api/holo/cancel")
    async def holo_cancel():
        return holo_runner.cancel()

    @app.post("/api/holo/test")
    async def holo_test(payload: dict = None):
        """Probe an OpenAI-compatible endpoint (cloud or local) and return
        the model ids it serves — powers the 测试连接 button."""
        p = payload if isinstance(payload, dict) else {}
        base = (str(p.get("base_url") or "").strip()
                or os.environ.get("PET_HOLO_BASE_URL")
                or "https://api.hcompany.ai/v1/")
        key = str(p.get("api_key") or "").strip() or os.environ.get("PET_HOLO_API_KEY", "")
        return await holo_probe(base, key)

    @app.get("/api/plugins")
    async def plugins_state():
        """Kernel introspection (cordis-style observability).

        Loaded plugins with their tools/services/deps, plugins parked on
        missing services (coeffect waiting room), the service table and
        the central effect ledger — everything the self-evolution loop
        needs to explain itself.
        """
        return {
            "ok": True,
            "plugins": _pet_plugins.describe(),
            "pending": _pet_plugins.describe_pending(),
            # Organs the static policy refused, with the reason. Without this
            # a rejected plugin looks like one that simply never appeared.
            "refusals": _pet_plugins.policy_refusals(),
            "effects": _pet_plugins.effects_report(),
            "services": sorted(str(s) for s in _pet_plugins.services.keys()),
        }

    @app.post("/api/plugins/unload")
    async def plugins_unload(payload: dict = None):
        """Switch a plugin OFF — the file stays, the kernel remembers the
        intent so hot-reload does not immediately revive it."""
        name = str((payload or {}).get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "need name"}, status_code=400)
        return _pet_plugins.unload_by_name(name)

    @app.post("/api/plugins/load")
    async def plugins_load(payload: dict = None):
        """Re-activate a plugin that was switched off (or is parked)."""
        name = str((payload or {}).get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "need name"}, status_code=400)
        return _pet_plugins.load_by_name(name)

    @app.get("/api/plugins/config")
    async def plugins_config():
        """Per-plugin config schema + resolved values, for settings editors."""
        return {"ok": True, "plugins": _pet_plugins.config_report()}

    @app.get("/api/diagnostics")
    async def diagnostics():
        """Read-only: how many errors the chat pipeline has swallowed.

        Every `except Exception` on the hot path counts itself here rather than
        logging at debug and vanishing. Non-empty `swallowed_errors` means
        something is quietly not working — the model's plugin tool table once
        stayed empty for a week and the only trace was a debug line the INFO
        file logger never wrote.
        """
        return {"ok": True, "swallowed_errors": dict(_silent_failures)}

    @app.post("/api/plugins/config")
    async def plugins_set_config(payload: dict = None):
        """Persist config overrides for one plugin and hot-apply them."""
        p = payload if isinstance(payload, dict) else {}
        name = str(p.get("name") or "").strip()
        if not name:
            return JSONResponse({"ok": False, "error": "need name"}, status_code=400)
        values = p.get("values")
        if not isinstance(values, dict):
            return JSONResponse({"ok": False, "error": "values must be an object"}, status_code=400)
        return _pet_plugins.set_plugin_config(name, values)

    # ── self-evolution HTTP surface (human/UI) ───────────────────────
    # The pet's own tools are above; these endpoints let a human review,
    # approve plugin promotion, and roll it back. Core candidates stay
    # review-only in M1 and are refused by the promoter.
    def _evolve_http_ready():
        svc = _evolve_service()
        if svc is None:
            return None, JSONResponse(
                {"ok": False, "error": "evolve workbench unavailable"},
                status_code=503,
            )
        return svc, None

    def _evolve_http_error(exc: Exception):
        from .evolve.models import EvolveError, NotAllowed, NotFound, PromotionRefused

        if isinstance(exc, NotFound):
            status = 404
        elif isinstance(exc, (NotAllowed, PromotionRefused, EvolveError)):
            status = 400
        else:
            log.exception("evolve endpoint failed")
            return JSONResponse(
                {"ok": False, "error": "internal error"}, status_code=500
            )
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=status)

    @app.get("/api/evolve/inspect")
    async def evolve_inspect():
        svc, err = _evolve_http_ready()
        if err is not None:
            return err
        try:
            return {"ok": True, "report": await asyncio.to_thread(svc.inspect)}
        except Exception as exc:
            return _evolve_http_error(exc)

    @app.get("/api/evolve/source")
    async def evolve_source(path: str = "", start: int = 1, end: int | None = None):
        svc, err = _evolve_http_ready()
        if err is not None:
            return err
        try:
            text = await asyncio.to_thread(
                svc.read_source, path, start=max(1, start), end=end
            )
            return {"ok": True, "text": text}
        except Exception as exc:
            return _evolve_http_error(exc)

    @app.get("/api/evolve/candidates")
    async def evolve_candidates(limit: int = 20):
        svc, err = _evolve_http_ready()
        if err is not None:
            return err
        try:
            rows = await asyncio.to_thread(
                svc.list_candidates, max(1, min(100, limit))
            )
            return {"ok": True, "candidates": rows}
        except Exception as exc:
            return _evolve_http_error(exc)

    @app.post("/api/evolve/propose")
    async def evolve_propose(payload: dict = None):
        svc, err = _evolve_http_ready()
        if err is not None:
            return err
        p = payload if isinstance(payload, dict) else {}
        try:
            rec = await asyncio.to_thread(
                svc.propose,
                goal=str(p.get("goal") or ""),
                plan=str(p.get("plan") or ""),
                edits=p.get("edits"),
                kind=str(p.get("kind") or "core"),
                title=str(p.get("title") or ""),
            )
            return {"ok": True, "candidate": rec}
        except Exception as exc:
            return _evolve_http_error(exc)

    @app.get("/api/evolve/candidates/{candidate_id}")
    async def evolve_candidate(candidate_id: str):
        svc, err = _evolve_http_ready()
        if err is not None:
            return err
        try:
            return {"ok": True, "candidate": await asyncio.to_thread(svc.show, candidate_id)}
        except Exception as exc:
            return _evolve_http_error(exc)

    @app.post("/api/evolve/candidates/{candidate_id}/evaluate")
    async def evolve_candidate_evaluate(candidate_id: str, payload: dict = None):
        svc, err = _evolve_http_ready()
        if err is not None:
            return err
        p = payload if isinstance(payload, dict) else {}
        try:
            result = await asyncio.to_thread(
                svc.evaluate, candidate_id, full=bool(p.get("full"))
            )
            return {"ok": True, "eval": result}
        except Exception as exc:
            return _evolve_http_error(exc)

    @app.post("/api/evolve/candidates/{candidate_id}/discard")
    async def evolve_candidate_discard(candidate_id: str):
        svc, err = _evolve_http_ready()
        if err is not None:
            return err
        try:
            rec = await asyncio.to_thread(svc.discard, candidate_id)
            return {"ok": True, "candidate": rec}
        except Exception as exc:
            return _evolve_http_error(exc)

    @app.post("/api/evolve/candidates/{candidate_id}/promote")
    async def evolve_candidate_promote(candidate_id: str, payload: dict = None):
        svc, err = _evolve_http_ready()
        if err is not None:
            return err
        p = payload if isinstance(payload, dict) else {}
        approved_by = str(p.get("approved_by") or "").strip()
        if not approved_by:
            return JSONResponse(
                {"ok": False, "error": "plugin promotion needs approved_by"},
                status_code=400,
            )
        try:
            result = await asyncio.to_thread(
                svc.promote, candidate_id, approved_by=approved_by
            )
            return {"ok": True, "promotion": result}
        except Exception as exc:
            return _evolve_http_error(exc)

    @app.post("/api/evolve/rollback")
    async def evolve_rollback(payload: dict = None):
        svc, err = _evolve_http_ready()
        if err is not None:
            return err
        p = payload if isinstance(payload, dict) else {}
        promotion_id = str(p.get("promotion_id") or "").strip()
        if not promotion_id:
            return JSONResponse(
                {"ok": False, "error": "need promotion_id"}, status_code=400
            )
        try:
            result = await asyncio.to_thread(svc.rollback, promotion_id)
            return {"ok": True, "rollback": result}
        except Exception as exc:
            return _evolve_http_error(exc)

    mcp_manager.register_builtin({
        "name": "screen_click",
        "description": (
            "Perform a mouse action on a screen element. "
            "Supports left_click (default), right_click, double_click, scroll. "
            "The element must have a bbox [x1, y1, x2, y2] with normalized coordinates "
            "(0.0-1.0) from observe_screen. "
            "For scroll, set action='scroll' with optional direction ('up'/'down') and amount. "
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "element": {
                    "type": "object",
                    "description": "An OmniParser element object with bbox",
                    "properties": {
                        "type": {"type": "string", "description": "text or icon"},
                        "bbox": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 4,
                            "maxItems": 4,
                            "description": "normalized bbox [x1, y1, x2, y2]",
                        },
                        "content": {"type": "string", "description": "the element's text or label"},
                        "action": {
                            "type": "string",
                            "enum": ["left_click", "right_click", "double_click", "scroll"],
                            "description": "Mouse action to perform (default: left_click)",
                        },
                        "direction": {
                            "type": "string",
                            "enum": ["up", "down"],
                            "description": "Scroll direction (only for action=scroll, default: down)",
                        },
                        "amount": {
                            "type": "integer",
                            "description": "Scroll amount in notches (only for action=scroll, default: 3)",
                        },
                    },
                    "required": ["bbox"],
                }
            },
            "required": ["element"],
        },
        "handler": _handle_screen_click,
    })
    mcp_manager.register_builtin({
        "name": "observe_screen",
        "description": (
            "OCR the user's screen with OmniParser and return structured labels "
            "with normalized bbox coordinates [x1,y1,x2,y2]. "
            "USE ONLY when you need precise coordinates to CLICK or OPERATE a "
            "screen element (screen_click needs a bbox from here). "
            "Do NOT use this just to 'look at the screen' — you have eyes: "
            "use capture_screen to see the actual image instead. "
            "If consent is denied the tool returns an error — guide the user to "
            "enable screen permission in settings."
        ),
        "input_schema": {"type": "object", "properties": {}},
        "handler": _handle_observe_screen,
    })
    mcp_manager.register_builtin({
        "name": "capture_screen",
        "description": (
            "Capture a raw screenshot of the user's screen so you can look at "
            "it. USE THIS when the user asks you to look at their screen, see "
            "what they're doing, or understand the current UI. "
            "By default this is your current vision cone (±30°, 400px), not "
            "the whole desktop; pass full=true only when the user explicitly "
            "asks to see the entire screen. "
            "If the active chat model is text-only, the gateway routes the "
            "screenshot through the configured vision channel and returns a "
            "detailed description, so this tool still works for 'looking'. "
            "Unlike observe_screen (which runs OCR for click coordinates), this "
            "is the 'look' tool. "
            "Requires screen consent; if denied, tell the user to enable it in settings."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "full": {
                    "type": "boolean",
                    "description": (
                        "Only true when the user explicitly asks to see the "
                        "entire screen; default false (your vision cone)."
                    ),
                },
            },
        },
        "handler": _handle_capture_screen,
    })
    mcp_router = create_mcp_router(mcp_manager)
    app.include_router(mcp_router)

    # ── Builtin tool: memory (cross-session long-term memory) ──────────
    # Lets the model write durable facts to MEMORY.md / USER.md. The frozen
    # snapshot is injected into the system prompt on every chat turn via
    # /api/memory (read by the renderer's fetchSkillsContext). This is the
    # mechanism that makes the pet "remember" across restarts.
    mcp_manager.register_builtin({
        "name": MEMORY_TOOL_SCHEMA["name"],
        "description": MEMORY_TOOL_SCHEMA["description"],
        "input_schema": MEMORY_TOOL_SCHEMA["input_schema"],
        "handler": memory_tool_handler,
    })

    # ── Builtin tools: decision roll + override ────────────────────────
    # The LLM proposes options with gut weights, Laya supplies probabilities,
    # the sampler rolls from the top-3 posterior. Descriptions must be
    # self-contained: the renderer only shows description text to most
    # text-MCP models, so the WHEN/HOW/example all live here.
    mcp_manager.register_builtin({
        "name": "pet_decision_roll",
        "description": (
            "「掷骰」决策工具：拿不准时让直觉层替你选一次，而不是硬想。\n"
            "WHEN 用：不确定用户意图、语气、下一步动作、该说多深、要不要开口的时候。\n"
            "HOW：列出 2-6 个选项，每个给 id（只用字母数字 _-，如 observe/chat）、"
            "中文 label、直觉权重 0.05-1（越想做越大）；Laya 潜意识层会给出概率并掷一次骰子，"
            "概率最低的尾巴选项不会被选中。\n"
            "它很快：直接调用，不要先长篇推理。结果只是一次采样——可以照做，也可以否决；"
            "如果不照做，用 pet_override_decision 记一笔。\n"
            '示例：[MCP:builtin/pet_decision_roll:{"question":"接下来做什么",'
            '"options":[{"id":"observe","label":"看屏幕","weight":0.8},'
            '{"id":"chat","label":"聊天","weight":0.3}]}]'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "要决定的简短问题（<=300 字）",
                },
                "options": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 6,
                    "description": "2-6 个候选，按直觉给权重",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "ASCII 短 id，只用字母数字 _-，如 observe",
                            },
                            "label": {
                                "type": "string",
                                "description": "给用户看的中文选项名，如 看屏幕",
                            },
                            "weight": {
                                "type": "number",
                                "description": "直觉权重 0.05-1，默认 1",
                            },
                        },
                        "required": ["id", "label"],
                    },
                },
                "context": {
                    "type": "string",
                    "description": "可选：一两句情境（桌面上开着什么、刚才发生了什么）",
                },
                "seed": {
                    "type": "integer",
                    "description": "可选：想重掷时换一个整数",
                },
            },
            "required": ["question", "options"],
        },
        "handler": _decision_roll_tool,
    })
    mcp_manager.register_builtin({
        "name": "pet_override_decision",
        "description": (
            "记录一次否决：当你故意不按 pet_decision_roll 的结果走时调用。"
            "参数：roll_id（掷骰结果里的 roll_id）、choice（你实际选的那个）、"
            "reason（一句短原因，比如「用户在哭，先安慰」）。"
            "它只写日志供日后学习，不会改变这一轮的任何东西。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "roll_id": {"type": "string", "description": "掷骰结果的 roll_id"},
                "choice": {"type": "string", "description": "你实际选择的 id 或行为"},
                "reason": {"type": "string", "description": "一句短原因"},
            },
            "required": ["roll_id", "choice", "reason"],
        },
        "handler": _decision_override_tool,
    })

    # ── Builtin tools: slow intuition + active D gut check ─────────────
    # Descriptions must be self-contained: most text-MCP providers only see
    # this text, so WHEN / HOW / the round semantics all live here.
    mcp_manager.register_builtin({
        "name": "pet_enter_slow",
        "description": (
            "进入慢直觉：当你觉得这一轮需要想得更深时调用，把本轮剩下的思考升级。\n"
            "WHEN：问题复杂、拿不准、需要推理或多步计划，而当前是快速/无思考模式时。\n"
            "HOW：strength 可选 low（默认）/ high / max，越深越慢。\n"
            "它只升级这一轮接下来的思考，不是新的一轮；调用一次就够，不要反复调用。\n"
            '示例：[MCP:builtin/pet_enter_slow:{"strength":"high"}]'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "strength": {
                    "type": "string",
                    "enum": ["low", "high", "max"],
                    "description": "思考深度，默认 low",
                },
            },
        },
        "handler": _enter_slow_tool,
    })
    mcp_manager.register_builtin({
        "name": "pet_decision",
        "description": (
            "主动问一次 D（底层直觉）：就是每轮开始前替你拿主意的同一套直觉。\n"
            "WHEN：不确定该想多深、回多长、接下来做什么时，直接问它一次，别硬猜。\n"
            "返回 thinking（no_thinking/low）、reaction（small/medium/large 及 max_tokens）、"
            "action（speak/walk/look/quiet/other）、decision_id。\n"
            "可以给 context（一两句情境）和 action_options（2-8 个 {id,label,weight} 临时选项）。\n"
            "它只是一次采样：可以照做，也可以改。不会改写你的生命总结。\n"
            '示例：[MCP:builtin/pet_decision:{"context":"用户在写代码"}]'
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "context": {
                    "type": "string",
                    "description": "可选：一两句当前情境",
                },
                "action_options": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 8,
                    "description": "可选：临时动作选项",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "ASCII 短 id，只用字母数字 _-",
                            },
                            "label": {"type": "string", "description": "中文动作名"},
                            "weight": {
                                "type": "number",
                                "description": "直觉权重 0-1，默认 1",
                            },
                        },
                        "required": ["id", "label"],
                    },
                },
            },
        },
        "handler": _decision_tool,
    })

    # ── Builtin tool: recall (LingLing model-initiated memory recall) ──
    # 现在由可插拔器官 organ_recall 提供（它用 ctx.tool() 注册同名工具）。
    # 只有器官缺席时才在这里兜底注册 —— 这样"停用器官"对模型是真的失去
    # 这个能力，而不是换了个实现偷偷继续工作。
    if not _pet_plugins.get_service("recall"):
        mcp_manager.register_builtin({
            "name": RECALL_TOOL_SCHEMA["name"],
            "description": RECALL_TOOL_SCHEMA["description"],
            "input_schema": RECALL_TOOL_SCHEMA["input_schema"],
            "handler": recall_tool_handler,
        })

    # ── Builtin tool: skill_create (external distillation write surface) ─
    # Lets the model persist a workflow it just executed (or one the user
    # described via /learn) as an auditable SKILL.md. This is the 创建 step
    # of the 引导→创建→索引→加载→Patch closed loop — the capability is a
    # text file, not a weight, so it's readable, editable, and migratable.
    async def _handle_skill_create(args: dict) -> dict:
        result = skill_service.create_skill(
            str(args.get("name") or "").strip(),
            str(args.get("description") or "").strip(),
            str(args.get("body") or ""),
            tags=args.get("tags") if isinstance(args.get("tags"), list) else None,
            version=str(args.get("version") or "0.1.0").strip() or "0.1.0",
            overwrite=bool(args.get("overwrite", False)),
        )
        is_error = not result.get("ok", False)
        summary = result.get("error") or f"skill saved: {result.get('name')}"
        return {
            "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
            "is_error": is_error,
            "summary": summary,
        }
    mcp_manager.register_builtin({
        "name": "skill_create",
        "description": (
            "Create a reusable skill (SKILL.md) from a workflow you just executed "
            "or one the user described. This is how you turn a one-off procedure "
            "into a durable, auditable capability — external distillation. "
            "Arguments: name (lowercase-hyphenated), description (one sentence, "
            "what it does), body (full Markdown with frontmatter-less sections: "
            "When to Use / Prerequisites / How to Run / Steps / Pitfalls / Verification). "
            "Optional: tags (list), version (default 0.1.0), overwrite (default false). "
            "Refuse to overwrite an existing skill unless overwrite=true."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "lowercase-hyphenated, <=64 chars"},
                "description": {"type": "string", "description": "one sentence, <=80 chars, states the capability"},
                "body": {"type": "string", "description": "full Markdown body WITHOUT frontmatter (we add it)"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "version": {"type": "string", "default": "0.1.0"},
                "overwrite": {"type": "boolean", "default": False},
            },
            "required": ["name", "description", "body"],
        },
        "handler": _handle_skill_create,
    })

    @app.get("/api/memory")
    async def get_memory_snapshot():
        """Return the model-owned 「记忆」 folder for system-prompt injection.

        The renderer calls this once per fetchSkillsContext cache window and
        injects the folder tree + the 记忆.md main file into the chat system
        prompt. Mid-session tool writes persist to disk immediately; the tree
        is read live so the model always sees its own latest structure.
        """
        files = mem_ctx.memory_files()
        return {
            "memory_dir": str(files.root),
            "tree": files.tree(),
            "root_md": files.read_main(),
            "theme": mem_ctx.current_theme,
        }

    @app.post("/api/memory/search")
    async def search_memory(payload: dict = None):
        """Search the current theme's events, memory files, and day logs."""
        body = payload or {}
        theme_memory_dir = mem_ctx.theme_dir(mem_ctx.current_theme)
        return _memory_search.search(
            str(body.get("query") or ""),
            event_store,
            theme_memory_dir,
            limit=body.get("limit", _memory_search.DEFAULT_LIMIT),
            scope=str(body.get("scope") or "all"),
        )

    @app.get("/api/memory/stats")
    async def get_memory_stats():
        snapshot = telemetry.snapshot()
        loop = _decision_loop_ref
        task = getattr(loop, "_task", None) if loop is not None else None
        snapshot.update({
            "memory": {
                "theme": mem_ctx.current_theme,
                "event_count": event_store.event_count(),
                "total_weight": event_store.total_weight(),
                "core_count": sum(1 for evt in event_store.get_all_events() if evt.get("core")),
            },
            "mood": {
                "current_mood": mood_store.current_mood,
                "intensity": mood_store.intensity,
                "emotion_index": mood_store.emotion_index,
            },
            "decision": {
                "initialized": loop is not None,
                "running": bool(task is not None and not task.done()),
                "ticks": getattr(loop, "_ticks", 0) if loop is not None else 0,
                "last_error": getattr(loop, "last_error", "") if loop is not None else "",
            },
        })
        return snapshot

    @app.post("/api/memory/switch")
    async def switch_memory(payload: dict):
        """Hot-switch the soul-layer memory to another animation theme
        (换形象，不换自我). Capability layer (skills/experiences) stays
        global."""
        nonlocal memory_store, event_store, mood_store
        theme = str((payload or {}).get("theme") or "").strip()
        slug = mem_ctx.switch(theme or None)
        memory_store = mem_ctx.memory_store
        event_store = mem_ctx.event_store
        mood_store = mem_ctx.mood_store
        # cordis 注入的服务跟着换店（灵魂层按主题切换，插件拿到的
        # 必须是当前主题的 store，不能是启动时的旧指针）
        _pet_plugins.services["mood"] = mood_store
        _pet_plugins.services["events"] = event_store
        _pet_plugins.services["memory"] = memory_store
        # 换身体：瞬时记忆/身体日志属于当前身体，跟着换店（旧身体的
        # 长期记忆仍在 themes/<slug>/ 里，不受影响）。
        try:
            if _ephemeral_ref is not None:
                _ephemeral_ref.memory_dir = mem_ctx.theme_dir(mem_ctx.current_theme)
                _ephemeral_ref.clear()
                _ephemeral_ref.refresh_dir_snapshot()
            if _body_log_ref is not None:
                _body_log_ref.items.clear()
            if _intuition_registry_ref is not None and _ephemeral_ref is not None:
                _intuition_registry_ref.clear_custom()
                _intuition_registry_ref.load_custom(
                    Path(_ephemeral_ref.memory_dir) / "intuitions.json"
                )
        except Exception:
            pass
        # 换身体的生命体征（soul isolation）：电量/血量/睡眠 + 500 轮台账
        # 属于当前身体，跟着主题换店——否则新灵魂继承旧身体的电量/HP/睡
        # 眠，台账也会跨灵魂串写。Rebuild both singletons on the new theme
        # dir; each loads that theme's own vitals.json / ledger.json (per-
        # soul state), and the old theme's files stay untouched on disk.
        try:
            _new_vitals_dir = mem_ctx.theme_dir(mem_ctx.current_theme)
            if get_vitals() is not None and get_ledger() is not None:
                _new_vitals = _Vitals(_new_vitals_dir)
                _new_ledger = _Ledger(_new_vitals_dir)
                set_vitals(_new_vitals)
                set_ledger(_new_ledger)
                log.info(
                    "vitals/ledger switched to theme %s (battery=%d%% rounds=%d)",
                    slug, round(_new_vitals.percent()), _new_ledger.round_count(),
                )
            _vitals_ref["dir"] = _new_vitals_dir  # _vitals_state_dir() follows the body (graveyard_archive)
        except Exception as exc:
            log.warning("vitals/ledger theme switch failed (continuing): %s", exc)
        _pet_plugins.emit("theme_switched", theme=slug)
        return {
            "ok": True,
            "theme": slug,
            "memory_dir": str(mem_ctx.theme_dir(slug)),
        }

    @app.post("/api/compress")
    async def compress_conversation(payload: dict = None):
        """睡眠压缩：把某天的对话日志压缩成记忆条 + 关键词。

        裸模型调用（无 persona / 记忆注入）——唯一指令是压缩。
        产物：潜意识记忆（EventStore）+ `memories/keywords.json`。
        body: {"day": "YYYY-MM-DD"}（缺省今天）
        """
        day = None
        try:
            day = str((payload or {}).get("day") or "").strip() or None
        except Exception:
            day = None
        try:
            from .memory import conversation_log, sleep_compress
            # Same theme-scoped root as events.json: compressing one body's
            # day into another body's soul would split the self.
            memory_dir = mem_ctx.theme_dir(mem_ctx.current_theme)
            _req = ChatRequest(messages=[], system=None, tools_enabled=False, silent=True)
            _provider = _resolve_provider(provider_registry, _req, server, state, log, None)
            result = await sleep_compress.compress_day(_provider, day, memory_dir=memory_dir)
            if result.get("status") == "ok":
                try:
                    result["persisted"] = sleep_compress.persist(
                        result["memories"], result["keywords"], event_store,
                        memory_dir=memory_dir, day=result.get("day"),
                    )
                    # Drop raw logs older than the retention window only after
                    # a successful compression (prune() was previously dead).
                    result["pruned"] = conversation_log.prune(7, memory_dir=memory_dir)
                except Exception as exc:
                    result["persist_error"] = str(exc)
            return result
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    # ── LingLing episodic memory endpoints ───────────────────────────

    @app.get("/api/events/context")
    async def get_events_context():
        """Build the episodic memory context for system-prompt injection.

        Returns the two-layer loaded context (top-5 + flashback + faded
        directory) that the renderer prepends to the system prompt.
        """
        context = _organ_service("loader", _loader_module).build_memory_context(event_store)
        return {
            "context": context,
            "event_count": event_store.event_count(),
            "total_weight": event_store.total_weight(),
        }

    @app.get("/api/events/list")
    async def list_events():
        """Structured event list for the Settings → Memory viewer.

        Read-only snapshot of the CURRENT theme's episodic memory,
        sorted by weight (highest first), with the fields the model
        judges and the system derives.
        """
        events = sorted(
            event_store.get_all_events(),
            key=lambda e: e.get("weight", 0),
            reverse=True,
        )
        return {
            "events": [
                {
                    "title": e.get("title", ""),
                    "content": e.get("content", ""),
                    "weight": e.get("weight", 0),
                    "emotion": e.get("emotion", ""),
                    "type": e.get("type", "experience"),
                    "resolved": e.get("resolved", True),
                    "core": e.get("core", False),
                    "created_at": e.get("created_at", ""),
                    "access_count": e.get("access_count", 0),
                    "decay_coefficient": e.get("decay_coefficient", 1.0),
                }
                for e in events
            ],
            "count": len(events),
            "theme": mem_ctx.current_theme,
        }

    # ── LingLing: shared extraction core ─────────────────────────────
    # One idempotent apply-path for both triggers: the gateway-side
    # death-note hook (runs first, so a client that died mid-stream
    # loses nothing) and the renderer's POST after stream end.
    _EXTRACTION_DEDUP_SECONDS = 600
    _last_extraction: dict = {"digest": "", "at": 0.0}

    def _apply_extraction(response_text: str) -> dict:
        """Parse the <<<MEM>>> block out of a reply and apply it.

        Idempotent by normalized-text dedup: the death-note hook and the
        renderer's POST carry the same reply text, so the second apply
        is a no-op within the window. Synchronous on purpose — the
        death note must be able to run during generator teardown.
        """
        result: dict = {
            "ok": True, "events_added": [], "mood_updated": False,
            "duplicate": False,
        }
        text = response_text or ""
        digest = hashlib.sha1(" ".join(text.split()).encode("utf-8")).hexdigest()
        now_ts = time.time()
        if (
            digest == _last_extraction["digest"]
            and (now_ts - _last_extraction["at"]) < _EXTRACTION_DEDUP_SECONDS
        ):
            result["duplicate"] = True
            telemetry.inc("memory.extraction.duplicates")
            return result

        telemetry.inc("memory.extraction.turns")
        try:
            data = parse_event_block(text)
            if data is None:
                telemetry.inc("memory.extraction.missing_block")
            if data:
                # Engagement proxy: rough token size of this conversation
                # turn (chars/4 ≈ tokens, no tokenizer needed). Feeds the
                # event's consolidation weighting (deep talk → harder to
                # forget; perfunctory → fades).
                conversation_tokens = len(text) // 4
                for evt_data in data.get("events", []):
                    events_before = event_store.event_count()
                    evt = event_store.add_event(
                        title=evt_data.get("title", ""),
                        content=evt_data.get("content", ""),
                        weight=evt_data.get("weight", 300),
                        emotion=evt_data.get("emotion"),  # None → derive from flow
                        type_=evt_data.get("type", "experience"),
                        resolved=evt_data.get("resolved", True),
                        core=bool(evt_data.get("core", False)),
                        conversation_tokens=conversation_tokens,
                    )
                    if event_store.event_count() > events_before:
                        telemetry.inc("memory.extraction.events")
                    result["events_added"].append(evt["id"])
                _organ_service("resonance", _resonance_module).mark_stale()

                # Core memory bank: the model's explicit picks (by title)
                # take priority, then very high-weight events auto-promote
                # while room remains (cap 7 — nearly-immortal memories must
                # stay rare). Shared with the dream consolidation cycle.
                promoted = promote_core(event_store, data.get("core") or [])
                if promoted:
                    log.info("Core bank: promoted %d event(s)", promoted)

                mood_data = data.get("mood", {})
                if mood_data:
                    mood_store.update_mood(
                        mood=mood_data.get("mood", "平静"),
                        intensity=mood_data.get("intensity", 40),
                        reason=mood_data.get("reason", ""),
                        changed=mood_data.get("changed", False),
                    )
                    result["mood_updated"] = True
        except Exception as exc:
            log.warning("Event extraction parse failed: %s", exc)

        _last_extraction["digest"] = digest
        _last_extraction["at"] = now_ts
        return result

    global _extraction_hook
    _extraction_hook = _apply_extraction

    # ── 摸头/戳 的反应词：快直觉（思考档）写一句，壳直接显示 ───────────
    # 以前每次 touch 都唤醒整颗主脑写"嗷"——贵、慢、还要等 20 秒首字。
    # 反应词是典型的"其余工作"：有判断（撒娇/打招呼/不耐烦）但只出一句，
    # 所以走小模型的思考档（reflect 同款），并把代价记在直觉账上。
    async def _maybe_touch_reaction(detail: Optional[str]) -> str:
        if not detail:
            return ""
        cfg = _reflex_config_ref.get("cfg")
        if cfg is None or not bool(cfg.enabled):
            return ""
        # 睡着/停机/死亡时不拿廉价反应词去吵醒身体：睡眠有自己的吵醒阶梯。
        vitals = get_vitals()
        if vitals is not None and (
            bool(vitals.forced_off) or bool(vitals.died) or vitals.is_asleep()
        ):
            return ""
        now = time.time()
        if now - float(_TOUCH_REACTION_STATE.get("last_at") or 0.0) < _TOUCH_REACTION_MIN_GAP_S:
            return ""
        try:
            mood_idx = float(mood_store.emotion_index) if mood_store is not None else 0.0
        except (TypeError, ValueError):
            mood_idx = 0.0
        thoughts: list = []
        try:
            lg = get_ledger()
            if lg is not None:
                thoughts = [
                    str(e.get("text") or "").strip()
                    for e in lg.entries()
                    if e.get("kind") == "fast"
                ][-2:]
        except Exception:
            thoughts = []
        result = await _reflex_module.touch_reaction(
            detail,
            cfg,
            mood=_mood_label_for_reflex(mood_idx),
            thoughts=thoughts,
        )
        text = str(result.get("text") or "").strip()
        if not text:
            log.debug(
                "touch reaction skipped: %s", result.get("failure") or "empty"
            )
            return ""
        _TOUCH_REACTION_STATE["last_at"] = now
        log.info(
            "touch reaction: %r (lat=%sms model=%s)",
            text, result.get("latency_ms"), result.get("model"),
        )
        try:
            if vitals is not None:
                vitals.drain_small(400)  # 直觉车道 ×0.5：约 200 单位
        except Exception as exc:
            _note_failure("touch_reaction_drain", exc)
        return text

    @app.post("/api/pet/interaction")
    async def pet_interaction(payload: PetInteractionRequest):
        """身体感受上报: the pet BODY reports touch (drag / click burst).

        Fresh sensations land in the module-level somatic buffer and are
        injected into the next chat's system prompt; meaningful ones also
        become light episodic events (throttled per kind so a gesture
        storm can't flood the memory stream).

        The response optionally carries a `reaction`: one short line written
        by the fast-intuition small model in thinking mode (the cheap path
        the shell uses instead of waking the whole main brain for "嗷").
        Empty string means "no cheap line" — the shell then falls back to
        its own touch-wake round.
        """
        detail, ev_title, ev_content = _organ_service("somatic", _somatic_module).record(
            _somatic_buffer, payload.model_dump()
        )
        if ev_title:
            try:
                store = get_event_store()
                if store is not None:
                    store.add_event(
                        title=ev_title,
                        content=ev_content,
                        weight=_somatic_module.EVENT_WEIGHT,
                        type_="experience",
                        resolved=True,
                    )
            except Exception as exc:
                log.warning("somatic event write failed: %s", exc)
        reaction = ""
        try:
            reaction = await _maybe_touch_reaction(detail)
        except Exception as exc:  # a reaction must never break a touch report
            log.debug("touch reaction failed: %s", exc)
        return {"ok": True, "recorded": bool(detail), "reaction": reaction}

    @app.post("/api/events/extract")
    async def extract_events(payload: dict):
        """Extract events from the last conversation turn.

        Called by the renderer after the streaming reply completes. The
        gateway-side death-note hook has usually applied this same reply
        already (idempotent — this call is then a no-op).
        Body: { "response_text": "..." }
        """
        response_text = str(payload.get("response_text") or "")
        result = _apply_extraction(response_text)

        # End-of-conversation housekeeping: consolidate mentioned events.
        # The decay organ listens on the kernel bus (was a direct module
        # call; now any plugin can react to conversation_end).
        _pet_plugins.emit("conversation_end")

        return {
            "ok": True,
            "events_added": result.get("events_added", []),
            "mood_updated": result.get("mood_updated", False),
            "duplicate": result.get("duplicate", False),
            "mood": mood_store.current_mood,
            "mood_intensity": mood_store.intensity,
        }

    @app.get("/api/mood")
    async def get_mood():
        """Return the current mood state."""
        return {
            "mood": mood_store.current_mood,
            "intensity": mood_store.intensity,
            "reason": mood_store.reason,
            "since": mood_store.since,
            "params": mood_store.get_params(),
            "emotion_tag": mood_store.get_emotion_tag(),
        }

    @app.get("/api/mood/context")
    async def get_mood_context():
        """Legacy endpoint: mood is internal state and has no LLM-facing
        self-report, so this intentionally returns an empty context."""
        return {"context": ""}

    @app.get("/api/prompt/system")
    async def get_system_prompt(provider: Optional[str] = None, compact: bool = False):
        """Canonical persona + memory contract (B-6, single source of truth).

        Shells fetch this instead of hardcoding the personality; per-turn
        dynamic blocks are still layered on by the caller.

        ``?provider=<name>`` selects the native-tool wording when that
        provider supports function calling. ``?compact=1`` selects the short
        text-MCP contract used by the browser-automation web provider, which
        re-sends the prompt on every request. Unknown names (and the default)
        keep the text-MCP contract — a lookup miss must never turn into a 500.
        """
        from . import persona
        native_tools = False
        name = (provider or "").strip()
        if name:
            try:
                candidate = provider_registry.get(name)
                native_tools = bool(getattr(candidate, "supports_function_calling", False))
            except Exception:
                native_tools = False
        prompt = (
            persona.compact_prompt_block(native_tools=native_tools)
            if compact else persona.base_prompt_block(native_tools=native_tools)
        )
        return {
            "ok": True,
            "version": persona.PROMPT_VERSION,
            "native_tools": native_tools,
            "compact": bool(compact),
            "prompt": prompt,
        }

    @app.post("/api/events/resonance")
    async def check_resonance(payload: dict):
        """Check if a user message resonates with stored events.

        Called by the renderer before sending the chat request.
        Returns any resonant events to inject into the system prompt.
        Body: { "message": "user's latest message" }
        """
        message = str(payload.get("message") or "")
        results = await _organ_service("resonance", _resonance_module).find_resonance(event_store, message)
        return {
            "resonant": [
                {"id": e["id"], "title": e["title"], "content": e["content"]}
                for e in results
            ],
            "count": len(results),
        }

    @app.get("/api/brain/stream")
    async def brain_stream():
        """Structured "brain stream" (rounds, D, tools, inner voice).

        Sends the recent ring buffer on connect, then live publishes. A
        comment heartbeat keeps idle proxies/renderers from timing out.
        """

        async def stream():
            queue = _brain.subscribe()
            try:
                snapshot = _brain.snapshot()
                last_id = snapshot[-1]["id"] if snapshot else 0
                for event in snapshot:
                    yield _sse(event)
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield b": ping\n\n"
                        continue
                    # Events published between subscribe() and snapshot()
                    # are already in the snapshot; skip the duplicate.
                    event_id = event.get("id", 0) if isinstance(event, dict) else 0
                    if event_id and event_id <= last_id:
                        continue
                    if event_id:
                        last_id = event_id
                    yield _sse(event)
            finally:
                _brain.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/providers")
    async def list_providers():
        return {
            "providers": provider_registry.list(),
            "default": "local",
        }

    @app.post("/api/providers/reload")
    async def reload_providers():
        """Reload API providers from ~/.pet/providers.json."""
        _register_providers()
        return {"ok": True, "providers": provider_registry.list()}

    # Model roots used by /api/models — honour the env override the
    # Electron host sets to <userData>/models/ in packaged mode.
    env_root = os.environ.get("PET_MODEL_DIR")
    extra_roots: List[Path] = []
    if env_root:
        extra_roots.append(Path(env_root))
    if initial_model:
        extra_roots.append(Path(initial_model).expanduser().resolve().parent)
    extra_roots.extend(_default_model_roots())

    def _get_active_model_path() -> Path:
        if server.model_path:
            return server.model_path
        if initial_model:
            return Path(initial_model)
        # Fall back to the first discovered gguf so /api/update-check
        # always has *some* anchor to compare against.
        items = discover_models(extra_roots)
        if items:
            return Path(items[0]["path"])
        # Last resort: synthesise a stub path so updater code can still
        # compute target_dir for download staging.
        return (extra_roots[0] if extra_roots else Path.cwd()) / "pet.gguf"

    updater = ModelUpdater(_get_active_model_path(), source=update_source)

    # llama.cpp engine self-update (binary, not weights).
    engine_updater = SidecarUpdater()

    # ─── Health / introspection ────────────────────────────────────────

    @app.get("/api/health")
    async def health():
        sub_health = await server.health()
        backend = detect_backend()
        adapter = state["current_adapter"]
        current = backend.get("current") or backend["recommended"]
        return {
            "ok": True,
            "alive": server.alive,
            "backend": "llama.cpp",
            "accel": current,
            "device": current,  # alias used by older Electron code paths
            "dtype": "gguf",
            "has_vision": bool(getattr(server, "mmproj_path", None)),
            "model_dir": str(server.model_path) if server.model_path else None,
            "model_name": server.model_path.name if server.model_path else None,
            "adapter": str(adapter) if adapter else None,
            "persona": _persona_for(adapter) if adapter else "default",
            "llama_server": sub_health,
            "port": server.port,
            "startup_error": startup_error,
            # Explicit so Electron's isHealthy can tell "engine off by the
            # user's choice" (gateway fully usable for providers) apart
            # from "engine still warming up" — otherwise it treats every
            # healthy sidecar as unhealthy and kill/respawns it in a loop
            # the moment the active model is a remote one.
            "engine_enabled": _engine_enabled(),
            # ── 生命体征 + 台账（renderer HUD learns the battery/sleep
            # state here; the sleeping animation itself is the renderer
            # phase's job）──
            "vitals": _vitals_health(),
            "ledger": _ledger_health(),
        }

    @app.get("/api/devices")
    def list_devices():
        info = detect_backend()
        return info

    @app.post("/api/set-device")
    async def set_device(payload: dict):
        device = str(payload.get("device") or "").strip().lower()
        if device not in ("metal", "cuda", "cpu", "vulkan", "mps", "auto", ""):
            return JSONResponse({"error": f"unknown device: {device!r}"}, status_code=400)
        # "mps" is the legacy name for Apple Silicon; transparently map
        # to metal for consistency with llama.cpp terminology.
        if device == "mps":
            device = "metal"
        if device == "vulkan" and platform.system() != "Windows":
            return JSONResponse(
                {"error": "vulkan backend is only configurable on Windows"},
                status_code=400,
            )
        if device:
            os.environ["PET_DEVICE"] = device
        else:
            os.environ.pop("PET_DEVICE", None)
        return {"ok": True, "device": device or "auto", "note": "restart sidecar to take effect"}

    @app.get("/api/onboarding")
    def onboarding():
        path = server.model_path or _get_active_model_path()
        present = path.exists() if path else False
        adapter = state["current_adapter"]
        backend = detect_backend()
        return {
            "model_present": present,
            "model_dir": str(path) if path else None,
            "device": backend.get("current") or backend["recommended"],
            "dtype": "gguf",
            "adapter": str(adapter) if adapter else None,
            "persona": _persona_for(adapter) if adapter else "default",
            "stage_hint": "ready" if present else "model-download",
        }

    # ─── Model / adapter listing ───────────────────────────────────────

    @app.get("/api/models")
    def list_models():
        items = discover_models(extra_roots)
        current = str(server.model_path) if server.model_path else None
        return {
            "items": items,
            "current": current,
            "current_name": server.model_path.name if server.model_path else None,
        }

    @app.post("/api/load-model")
    async def load_model(payload: dict):
        nonlocal startup_error
        path = str(payload.get("path") or "").strip()
        mmproj_path = payload.get("mmproj")
        mmproj_path = str(mmproj_path).strip() if mmproj_path else ""
        if not path:
            return JSONResponse({"error": "path is required"}, status_code=400)
        target = Path(path).expanduser().resolve()
        if not target.is_file() or target.suffix.lower() != ".gguf":
            return JSONResponse({"error": f"not a .gguf file: {target}"}, status_code=400)
        # Resolve mmproj: take the explicit value when provided, otherwise
        # look up a sibling `mmproj-*.gguf`. Empty string clears it —
        # CRITICAL: swap_model treats None as "keep the previous pairing",
        # so a model without a sibling mmproj MUST pass "" or the old
        # projector stays paired and crashes llama-server with an embd
        # mismatch (e.g. Nanbeige text model + Qwen3.5 mmproj).
        if mmproj_path:
            mmproj_target = Path(mmproj_path).expanduser().resolve()
            if not mmproj_target.is_file() or mmproj_target.suffix.lower() != ".gguf":
                return JSONResponse({"error": f"not a .gguf file: {mmproj_target}"}, status_code=400)
        else:
            sibling = find_sibling_mmproj(target)
            mmproj_target = sibling or ""  # "" clears; never None
        bridge.post("working", event="LoadModel", title=f"加载 {target.name}")
        try:
            await server.swap_model(target, mmproj_target)
            updater.local_model_path = target
            startup_error = None
        except Exception as exc:
            startup_error = str(exc)
            bridge.post("error")
            return JSONResponse({"error": str(exc)}, status_code=500)
        bridge.post("idle")
        return {
            "ok": True,
            "model_dir": str(target),
            "model_name": target.name,
            "mmproj_dir": str(mmproj_target) if mmproj_target else None,
            "mmproj_name": mmproj_target.name if mmproj_target else None,
        }

    @app.post("/api/engine/start")
    async def engine_start():
        # An explicit start is the user's intent too.
        _set_engine_enabled(True)
        """Manual engine start from the settings page. Uses whatever model
        the client is configured with; fails loudly when none is set (the
        user must pick a model first)."""
        if server.alive:
            return {"ok": True, "already": True}
        try:
            await server.start()
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        return {"ok": True}

    @app.post("/api/engine/stop")
    async def engine_stop():
        was = server.alive
        # Stopping IS the user saying "I want this off" - persist it so the
        # next pet restart does not quietly bring it back.
        _set_engine_enabled(False)
        await server.stop()
        return {"ok": True, "was_running": was}

    @app.post("/api/providers/discover-models")
    async def providers_discover_models(payload: dict):
        """CherryStudio-style: given a baseUrl + key, enumerate the models
        that endpoint actually serves, so nobody hand-copies model ids.

        Proxied through the sidecar (not fetched from the renderer) to
        dodge CORS - cloud APIs do not send CORS headers to app:// pages.
        Speaks the OpenAI-compatible /models shape, which LM Studio,
        Ollama (with the compat route), and nearly every aggregator also
        implement."""
        base = str((payload or {}).get("baseUrl") or "").strip().rstrip("/")
        key = str((payload or {}).get("apiKey") or "").strip()
        if not base:
            return JSONResponse({"ok": False, "error": "baseUrl is required"}, status_code=400)
        url = f"{base}/models"
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
        items = data.get("data") if isinstance(data, dict) else data
        ids: list[str] = []
        for it in items or []:
            mid = it.get("id") if isinstance(it, dict) else it
            if mid:
                ids.append(str(mid))
        ids.sort()
        return {"ok": True, "models": ids, "base": base}

    @app.get("/api/engine/params")
    async def engine_params():
        """Current runtime knobs + the exact argv they produce, so the
        Settings page can show reality instead of guessing."""
        return {
            "ok": True,
            "running": server.alive,
            "enabled": _engine_enabled(),
            "params": {
                "load_mode": server.load_mode,
                "cache_type_k": server.cache_type_k,
                "cache_type_v": server.cache_type_v,
                "flash_attn": server.flash_attn,
                "n_cpu_moe": server.n_cpu_moe,
                "batch_size": server.batch_size,
                "ubatch_size": server.ubatch_size,
                "ctx_size": server.ctx_size,
                "n_gpu_layers": server.n_gpu_layers,
                "threads": server.threads,
            },
            "argv": server.argv_preview,
        }

    @app.post("/api/engine/benchmark")
    async def engine_benchmark(payload: dict = None):
        """One-shot speed test: returns llama.cpp's own tokens/sec."""
        if not server.alive:
            return JSONResponse(
                {"ok": False, "error": "引擎未运行，先点「启动引擎」再测速"},
                status_code=409,
            )
        try:
            n = int((payload or {}).get("n_predict") or 128)
        except Exception:
            n = 128
        try:
            return await server.benchmark(n_predict=n)
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    @app.post("/api/upload-model")
    async def upload_model(file: UploadFile = File(...)):
        """Accept a .gguf file upload, save to models dir, and auto-load."""
        nonlocal startup_error
        if not file.filename or not file.filename.lower().endswith(".gguf"):
            return JSONResponse({"error": "only .gguf files are accepted"}, status_code=400)

        # Determine save directory: use the first available model root
        roots = _default_model_roots() + extra_roots
        save_dir = None
        for r in roots:
            try:
                rp = r.expanduser().resolve()
                if rp.exists() or rp.parent.exists():
                    rp.mkdir(parents=True, exist_ok=True)
                    save_dir = rp
                    break
            except Exception:
                continue
        if save_dir is None:
            save_dir = Path.cwd() / "models"
            save_dir.mkdir(parents=True, exist_ok=True)

        target = (save_dir / file.filename).resolve()
        # Write uploaded file
        try:
            with open(target, "wb") as f:
                while chunk := await file.read(1024 * 1024):  # 1 MiB chunks
                    f.write(chunk)
        except Exception as exc:
            return JSONResponse({"error": f"failed to save file: {exc}"}, status_code=500)

        # Auto-load the model
        mmproj_target = find_sibling_mmproj(target)
        bridge.post("working", event="LoadModel", title=f"加载 {target.name}")
        try:
            await server.swap_model(target, mmproj_target)
            updater.local_model_path = target
            startup_error = None
        except Exception as exc:
            startup_error = str(exc)
            bridge.post("error")
            return JSONResponse({"error": str(exc)}, status_code=500)
        bridge.post("idle")
        return {
            "ok": True,
            "model_dir": str(target),
            "model_name": target.name,
            "mmproj_dir": str(mmproj_target) if mmproj_target else None,
            "mmproj_name": mmproj_target.name if mmproj_target else None,
        }

    def _scan_adapters() -> List[dict]:
        # Re-resolve the root each call so Settings → "open adapter dir"
        # → drop new .gguf → "refresh" picks up files added at runtime
        # without restarting the sidecar. Also re-read the manifest
        # mirror on every call so rename / upload mutations show up in
        # the next /api/adapters response without any explicit refresh
        # ping from Electron.
        root = _resolve_adapter_root(server.model_path)
        if not root:
            return []
        items = discover_adapters([root])
        manifest = read_adapter_manifest(root)
        by_path = _manifest_by_resolved_path(manifest)
        for item in items:
            try:
                key = Path(item["path"]).expanduser().resolve()
            except Exception:
                continue
            entry = by_path.get(key)
            if not entry:
                continue
            # Only surface the product-layer fields; gateway's persona
            # slug already on `item` wins by default but a manifest
            # override (user typed their own) takes precedence.
            if isinstance(entry.get("displayName"), str) and entry["displayName"].strip():
                item["displayName"] = entry["displayName"].strip()
            if isinstance(entry.get("aliases"), list):
                item["aliases"] = [str(a).strip() for a in entry["aliases"] if str(a).strip()]
            if isinstance(entry.get("source"), str):
                item["source"] = entry["source"]
            if isinstance(entry.get("id"), str):
                item["id"] = entry["id"]
            if isinstance(entry.get("persona"), str) and entry["persona"].strip():
                item["persona"] = entry["persona"].strip()
        return items

    @app.get("/api/adapters")
    def list_adapters():
        items = _scan_adapters()
        current = state["current_adapter"]
        return {
            "items": items,
            "current": str(current) if current else None,
            "current_name": current.name if current else None,
            "adapter_dir": str(_resolve_adapter_root(server.model_path) or ""),
        }

    @app.post("/api/load-adapter")
    async def load_adapter(payload: dict):
        raw = payload.get("path")
        # path = null  →  deactivate any LoRA (back to base model)
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            # If llama-server was booted with `--lora <something>`, a
            # per-request `lora: []` is enough to force base output on
            # modern llama.cpp. We still respawn with no `--lora` here
            # so switching back to Base releases the adapter weights too.
            if server.adapter_paths:
                bridge.post("working", event="UnloadAdapter", title="卸载 LoRA")
                try:
                    await server.reload_adapters([])
                except Exception as exc:
                    bridge.post("error")
                    return JSONResponse({"error": str(exc)}, status_code=500)
                bridge.post("idle")
            state["current_adapter"] = None
            return {"ok": True, "adapter": None, "persona": "default"}

        target = Path(str(raw)).expanduser()
        try:
            target = target.resolve(strict=True)
        except FileNotFoundError:
            return JSONResponse(
                {"error": f"adapter file not found: {target}"},
                status_code=400,
            )
        if target.suffix.lower() != ".gguf":
            return JSONResponse(
                {"error": f"not a .gguf adapter: {target}"},
                status_code=400,
            )

        # If the requested adapter isn't currently `--lora`-loaded,
        # restart llama-server so that ONLY this adapter is loaded.
        # We deliberately don't keep a growing list of preloaded LoRAs
        # in memory — that was the old behaviour, and it meant any
        # third-party `.gguf` on disk silently rode along whether the
        # user wanted it or not. The user pays one sidecar restart
        # (~3-4s) per LoRA switch, which matches the cost of switching
        # base models and is the only honest way to keep memory tight.
        if server.adapter_id_for(target) is None:
            bridge.post("working", event="LoadAdapter", title=f"加载 {target.name}")
            try:
                await server.reload_adapters([target])
            except Exception as exc:
                bridge.post("error")
                return JSONResponse({"error": str(exc)}, status_code=500)
            bridge.post("idle")
            if server.adapter_id_for(target) is None:
                return JSONResponse(
                    {"error": f"llama-server refused adapter: {target}"},
                    status_code=500,
                )

        state["current_adapter"] = target
        return {
            "ok": True,
            "adapter": str(target),
            "persona": _persona_for(target),
        }

    @app.post("/api/classify")
    def classify_endpoint(payload: dict):
        return JSONResponse(
            {"error": "/api/classify not implemented for llama.cpp backend yet"},
            status_code=501,
        )

    # ─── Updater ───────────────────────────────────────────────────────

    @app.get("/api/update-check")
    async def update_check():
        updater.local_model_path = server.model_path or _get_active_model_path()
        return await asyncio.to_thread(updater.check)

    @app.post("/api/update-apply")
    async def update_apply():
        nonlocal startup_error
        updater.local_model_path = server.model_path or _get_active_model_path()

        async def stream():
            nonlocal startup_error
            queue: asyncio.Queue = asyncio.Queue()
            sentinel = object()
            loop = asyncio.get_running_loop()

            def producer():
                try:
                    for ev in updater.apply():
                        loop.call_soon_threadsafe(queue.put_nowait, ev)
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, sentinel)

            import threading as _t
            _t.Thread(target=producer, daemon=True).start()

            bridge.post("working", event="UpdateApply", title="正在更新模型")
            try:
                while True:
                    ev = await queue.get()
                    if ev is sentinel:
                        break
                    yield _sse(ev)
                    if ev.get("phase") == "complete":
                        try:
                            # Restart llama-server against the (potentially
                            # renamed) gguf so the new weights take effect
                            # without a full sidecar restart.
                            items = discover_models(extra_roots)
                            if items:
                                target = Path(items[0]["path"])
                                await server.swap_model(target)
                                updater.local_model_path = target
                                startup_error = None
                                yield _sse({"phase": "reloaded", "model": str(target)})
                        except Exception as exc:
                            startup_error = str(exc)
                            yield _sse({"phase": "reload-error", "message": str(exc)})
            finally:
                bridge.post("idle")

        return StreamingResponse(stream(), media_type="text/event-stream")

    # ─── Engine (llama.cpp binary) updater ─────────────────────────────

    @app.get("/api/engine-update-check")
    async def engine_update_check():
        return await asyncio.to_thread(engine_updater.check)

    @app.post("/api/engine-update-apply")
    async def engine_update_apply():
        """One-click llama-server binary update.

        Streams the same phase contract as /api/update-apply
        (start/transfer/swap/complete → reloaded). The engine is stopped
        before the file swap (Windows exe file locks) and restarted
        afterwards via callbacks bridged back into this event loop.
        """
        nonlocal startup_error
        loop = asyncio.get_running_loop()
        was_alive = server.alive

        async def _stop_server():
            if server.alive:
                await server.stop()

        async def _start_server():
            await server.start()
            startup_error = None

        def sync_stop() -> None:
            fut = asyncio.run_coroutine_threadsafe(_stop_server(), loop)
            fut.result(timeout=30)

        def sync_start() -> None:
            fut = asyncio.run_coroutine_threadsafe(_start_server(), loop)
            fut.result(timeout=90)

        async def stream():
            queue: asyncio.Queue = asyncio.Queue()
            sentinel = object()

            def producer():
                try:
                    for ev in engine_updater.apply(
                        stop_callback=sync_stop,
                        start_callback=sync_start if was_alive else None,
                    ):
                        loop.call_soon_threadsafe(queue.put_nowait, ev)
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, sentinel)

            import threading as _t
            _t.Thread(target=producer, daemon=True).start()

            bridge.post("working", event="EngineUpdate", title="正在更新推理引擎")
            try:
                while True:
                    ev = await queue.get()
                    if ev is sentinel:
                        break
                    yield _sse(ev)
                    if ev.get("phase") == "complete":
                        yield _sse({
                            "phase": "reloaded",
                            "engine": engine_updater.remote_version() or "?",
                        })
            finally:
                bridge.post("idle")

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/api/engine-update-apply-dir")
    async def engine_update_apply_dir(payload: dict):
        """Offline engine update from a user-provided folder (e.g. a copy
        of an official release someone else downloaded). The folder must
        contain a runnable llama-server newer than the installed one;
        it is copied, never modified. Same SSE phase contract as the
        online path."""
        nonlocal startup_error
        source_dir = str((payload or {}).get("source_dir") or "").strip()
        if not source_dir:
            return JSONResponse({"error": "source_dir is required"}, status_code=400)
        loop = asyncio.get_running_loop()
        was_alive = server.alive

        async def _stop_server():
            if server.alive:
                await server.stop()

        async def _start_server():
            await server.start()
            startup_error = None

        def sync_stop() -> None:
            fut = asyncio.run_coroutine_threadsafe(_stop_server(), loop)
            fut.result(timeout=30)

        def sync_start() -> None:
            fut = asyncio.run_coroutine_threadsafe(_start_server(), loop)
            fut.result(timeout=90)

        async def stream():
            queue: asyncio.Queue = asyncio.Queue()
            sentinel = object()

            def producer():
                try:
                    for ev in engine_updater.apply_from_dir(
                        source_dir,
                        stop_callback=sync_stop,
                        start_callback=sync_start if was_alive else None,
                    ):
                        loop.call_soon_threadsafe(queue.put_nowait, ev)
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, sentinel)

            import threading as _t
            _t.Thread(target=producer, daemon=True).start()

            bridge.post("working", event="EngineUpdate", title="正在从文件夹更新推理引擎")
            try:
                while True:
                    ev = await queue.get()
                    if ev is sentinel:
                        break
                    yield _sse(ev)
                    if ev.get("phase") == "complete":
                        yield _sse({"phase": "reloaded"})
            finally:
                bridge.post("idle")

        return StreamingResponse(stream(), media_type="text/event-stream")

    # ─── Chat ──────────────────────────────────────────────────────────

    @app.post("/api/warmup")
    async def warmup():
        if not server.alive:
            return JSONResponse({"ok": False, "error": "llama-server not running"}, status_code=503)
        t0 = time.time()
        try:
            await server.complete_once(prompt=" ", max_tokens=1)
            return {"ok": True, "elapsed_ms": int((time.time() - t0) * 1000)}
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    def _lora_arr_for(req: ChatRequest) -> Optional[List[dict]]:
        """Compute the per-request `lora` array.

        - disable_adapter=true  → []   (force base for this request)
        - active adapter set    → [{id, scale: 1.0}]
        - no adapter active     → []   (force base)

        Sending an empty list is intentionally explicit: llama.cpp
        treats adapters omitted from a per-request `lora` list as scale
        0.0, so base chat never depends on whatever global adapter scale
        the server happened to inherit at startup.
        """
        if req.disable_adapter:
            return []
        current = state["current_adapter"]
        if not current:
            return []
        idx = server.adapter_id_for(current)
        if idx is None:
            # State got out of sync (e.g. sidecar restarted without
            # re-registering this path). Fail open to base rather than
            # 500 — the user will notice the persona is gone and can
            # re-select from Settings.
            log.warning("active adapter %s missing from llama-server index", current)
            return []
        return [{"id": idx, "scale": 1.0}]

    @app.post("/api/chat")
    async def chat(req: ChatRequest):
        if not req.messages:
            return JSONResponse({"error": "messages is empty"}, status_code=400)

        # Debug log: record every chat request
        _sys_preview = ""
        if req.system:
            _sys_preview = req.system[:300].replace("\n", "\\n")
        _last_msg = ""
        if req.messages:
            _last_msg = str(req.messages[-1].content)[:200]
        log.info(
            "CHAT REQ | messages=%d system=%s provider=%s stream=%s | last_msg=%s",
            len(req.messages),
            _sys_preview if _sys_preview else "(none)",
            req.model_provider or "auto",
            req.stream,
            _last_msg,
        )

        lora_arr = _lora_arr_for(req)
        # 快直觉 reflex 配置随请求注入：build_app 启动解析的单例在
        # _stream_chat_provider（build_app 闭包之外）里经 get_reflex_config()
        # 读取。播种进本请求的 context——生成器（anyio start_soon 会复制
        # 调用者上下文）整轮可见；而直接调用生成器的测试路径（不经过
        # 路由）不会被动武装，reflex 门只对真实 HTTP 生命轮生效。
        # 注意：这里必须播种启动单例（_reflex_config_ref["cfg"]），
        # 不能调 get_reflex_config()——那读的是本请求的 ContextVar，
        # 路由入口处恒为 None，等于自己 seed 自己（2026-11-23 死枪 bug）。
        set_reflex_config_override(_reflex_config_ref.get("cfg"))
        # The raw conversation log belongs to the current theme's soul, so
        # sleep compression and events.json share one memory root.
        conversation_dir = mem_ctx.theme_dir(mem_ctx.current_theme)
        if req.stream:
            return StreamingResponse(
                _stream_chat_provider(
                    provider_registry, mcp_manager, bridge, req, server, state, mood_store, lora_arr,
                    conversation_dir,
                ),
                media_type="text/event-stream",
            )
        return JSONResponse(await _blocking_chat_provider(
            provider_registry, mcp_manager, bridge, req, server, state, mood_store, lora_arr,
            conversation_dir,
        ))

    @app.post("/api/debug/chat")
    async def debug_chat(payload: dict):
        """Non-streaming chat with full debug output. For AI debugging via web panel.

        Body: { "message": "text", "screen": true/false, "system": "optional override" }
        Returns: { "reply": "...", "system_used": "...", "screen_context": "...", "error": null }
        """
        message = str(payload.get("message") or "").strip()
        if not message:
            return JSONResponse({"error": "message is required"}, status_code=400)

        screen_context = None
        if payload.get("screen"):
            # Auto-trigger screen observation
            try:
                async with consent_lock:
                    pass  # don't consume consent in debug mode
                b64 = await asyncio.to_thread(screen_capture)
                ocr_mode = "local"
                parsed = []
                try:
                    status = await omniparser_manager.ensure_alive()
                    if status in ("ready", "alive"):
                        async with httpx.AsyncClient(timeout=300.0) as cli:
                            pr = await cli.post(
                                "http://127.0.0.1:8000/parse/",
                                json={"base64_image": b64, "chinese_ocr": True},
                            )
                            if pr.status_code == 200:
                                data = pr.json()
                                raw = data.get("parsed_content_list") or []
                                parsed = [str(item) for item in raw] if isinstance(raw, list) else [str(raw)]
                            else:
                                parsed = ["[OmniParser error]"]
                    else:
                        parsed = [f"[OmniParser not available: {status}]"]
                except Exception as exc:
                    parsed = [f"[OmniParser failed: {exc}]"]
                if parsed:
                    # Clean up: extract just the text content from each item
                    clean_lines = []
                    for item in parsed:
                        import ast as _ast
                        try:
                            # Items may be stringified dicts — parse them
                            d = _ast.literal_eval(item) if isinstance(item, str) and item.startswith("{") else item
                            if isinstance(d, dict):
                                content = d.get("content") or ""
                                if content:
                                    clean_lines.append(str(content))
                            elif item:
                                clean_lines.append(str(item))
                        except Exception:
                            clean_lines.append(str(item))
                    if clean_lines:
                        screen_context = "【用户当前屏幕内容】\n" + "\n".join(clean_lines)
            except Exception as exc:
                log.warning("debug screen observe failed: %s", exc)

        system = payload.get("system") or screen_context or None
        sys_preview = (system or "")[:200] if system else "(none)"
        log.info("DEBUG CHAT | message=%s | system=%s | screen=%s", message[:200], sys_preview, bool(screen_context))

        req = ChatRequest(
            messages=[{"role": "user", "content": message}],
            system=system,
            stream=False,
            max_new_tokens=4096,
            temperature=0.6,
        )
        lora_arr = _lora_arr_for(req)
        try:
            result = await _blocking_chat_provider(
                provider_registry, mcp_manager, bridge, req, server, state, mood_store, lora_arr,
            )
            reply = ""
            if isinstance(result, dict):
                reply = result.get("reply") or result.get("content") or result.get("text") or str(result)
            else:
                reply = str(result)
            log.info("DEBUG CHAT DONE | reply=%s", reply[:200])
            return {
                "reply": reply,
                "system_used": system,
                "screen_context": screen_context,
                "error": None,
            }
        except Exception as exc:
            log.exception("debug chat error: %s", exc)
            return JSONResponse({"error": str(exc), "reply": "", "system_used": system, "screen_context": screen_context}, status_code=500)

    @app.post("/api/state")
    def manual_state(payload: dict):
        state = str(payload.get("state") or "idle")
        bridge.post(state, event=payload.get("event"))
        return {"ok": True}

    # ── Screen observation ──────────────────────────────────────────
    VALID_CONSENT = {"deny", "once", "always"}

    @app.post("/api/screen/consent-status")
    async def set_consent(payload: dict):
        nonlocal consent_state, consent_lock
        value = payload.get("consent", "deny")
        if value not in VALID_CONSENT:
            return JSONResponse({"error": f"invalid consent: {value}"}, status_code=400)
        async with consent_lock:
            consent_state = value
        return {"current_consent": consent_state}

    @app.get("/api/screen/consent-status")
    async def get_consent():
        return {"current_consent": consent_state}

    @app.post("/api/screen/permission-respond")
    async def permission_respond(payload: dict):
        """Electron host replies to a per-action screen permission request
        (user clicked Yes/No on the authorization dialog)."""
        nonlocal consent_state, consent_lock
        request_id = str((payload or {}).get("request_id") or "")
        allow = bool((payload or {}).get("allow", False))
        remember = bool((payload or {}).get("remember", False))
        result = await permission_manager.respond(request_id, allow, remember)
        # Keep the legacy consent_state mirror in sync ("always").
        if remember:
            async with consent_lock:
                consent_state = "always"
        return result

    @app.post("/api/screen/observe")
    async def observe_screen(payload: dict = {}):
        nonlocal consent_state, consent_lock
        # Debug log: record every screen observe request
        log.info(
            "SCREEN OBSERVE | ocr_mode=%s chinese_ocr=%s consent=%s",
            payload.get("ocr_mode", "local"),
            payload.get("chinese_ocr", False),
            consent_state,
        )
        # Consume "once" permission inside the lock, before any await
        async with consent_lock:
            if consent_state == "deny":
                return JSONResponse({"error": "consent denied"}, status_code=403)
            consumed = consent_state == "once"
            if consumed:
                consent_state = "deny"

        # Capture screen (always attempt this first)
        try:
            b64 = await asyncio.to_thread(screen_capture)
        except Exception as exc:
            log.exception("screen capture failed: %s", exc)
            return JSONResponse({"error": f"screen capture failed: {exc}"}, status_code=500)

        ocr_mode = payload.get("ocr_mode", "local")
        parsed = []

        if ocr_mode == "api":
            api_url = payload.get("ocr_api_url", "").strip()
            api_key = payload.get("ocr_api_key", "").strip()
            api_model = payload.get("ocr_api_model", "").strip()
            if not api_url:
                parsed.append("[OCR API not configured]")
            else:
                try:
                    headers = {"Content-Type": "application/json"}
                    if api_key:
                        headers["Authorization"] = f"Bearer {api_key}"
                    body = {"image": b64}
                    if api_model:
                        body["model"] = api_model
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        resp = await client.post(api_url, json=body, headers=headers)
                        if resp.status_code == 200:
                            data = resp.json()
                            # Accept both list-of-strings and structured formats
                            if isinstance(data, list):
                                parsed = [str(item) for item in data]
                            elif isinstance(data, dict):
                                texts = data.get("text") or data.get("texts") or data.get("result") or data.get("content") or str(data)
                                if isinstance(texts, list):
                                    parsed = [str(t) for t in texts]
                                else:
                                    parsed = [str(texts)]
                            else:
                                parsed = [str(data)]
                        else:
                            parsed.append(f"[OCR API error: HTTP {resp.status_code}]")
                except Exception as exc:
                    log.warning("OCR API call failed: %s", exc)
                    parsed.append(f"[OCR API call failed: {exc}]")
        else:
            # Local mode: try OmniParser if running
            try:
                status = await omniparser_manager.ensure_alive()
                if status in ("ready", "alive"):
                    chinese = payload.get("chinese_ocr", False)
                    async with httpx.AsyncClient(timeout=300.0) as cli:
                        pr = await cli.post(
                            f"http://127.0.0.1:8000/parse/",
                            json={"base64_image": b64, "chinese_ocr": chinese},
                        )
                        if pr.status_code == 200:
                            data = pr.json()
                            raw = data.get("parsed_content_list") or data.get("texts") or []
                            if isinstance(raw, list):
                                parsed = [str(item) for item in raw]
                            else:
                                parsed = [str(raw)]
                        else:
                            parsed.append("[OmniParser error]")
                else:
                    parsed.append("[OmniParser not available]")
            except Exception as exc:
                log.warning("OmniParser call failed: %s: %s", type(exc).__name__, exc)
                parsed.append(f"[OmniParser call failed: {type(exc).__name__}]")

        if not parsed:
            parsed.append("[No text detected]")

        # Clean up: extract just the text content from each item
        clean_parsed = []
        for item in parsed:
            import ast as _ast
            try:
                d = _ast.literal_eval(item) if isinstance(item, str) and item.startswith("{") else item
                if isinstance(d, dict):
                    content = d.get("content") or ""
                    if content:
                        clean_parsed.append(str(content))
                elif item:
                    clean_parsed.append(str(item))
            except Exception:
                clean_parsed.append(str(item))
        if clean_parsed:
            parsed = clean_parsed

        log.info("SCREEN OBSERVE DONE | parsed=%d items, first=%s", len(parsed), parsed[0][:100] if parsed else "")

        return {
            "parsed_content_list": parsed,
            "som_image_base64": None,
            "raw_screenshot_base64": b64,
            "consent_consumed": consumed,
            "current_consent": consent_state,
            "timed_out": False,
        }

    @app.post("/api/screen/click")
    async def click_screen(payload: dict):
        nonlocal consent_state, consent_lock
        async with consent_lock:
            if consent_state == "deny":
                return JSONResponse({"error": "consent denied"}, status_code=403)

        element = payload.get("element")
        if not element:
            return JSONResponse({"error": "element is required"}, status_code=400)
        if "bbox" not in element:
            return JSONResponse({"error": "element must contain bbox"}, status_code=400)

        try:
            result = await screen_click.click_element(element)
            return result
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:
            log.warning("screen click failed: %s", exc)
            return JSONResponse({"error": f"screen click failed: {exc}"}, status_code=500)

    @app.get("/")
    def index():
        return JSONResponse({
            "ok": True,
            "note": "DeskPet sidecar gateway (llama.cpp backend)",
            "endpoints": [
                "/api/health", "/api/chat", "/api/warmup",
                "/api/models", "/api/load-model", "/api/upload-model",
                "/api/devices", "/api/set-device", "/api/onboarding",
                "/api/update-check", "/api/update-apply",
                "/api/engine-update-check", "/api/engine-update-apply", "/api/engine-update-apply-dir",
                "/api/adapters", "/api/load-adapter", "/api/classify",
                "/api/state",
                "/api/skills", "/api/skills/{name}", "/api/skills/learn",
                "/api/curator/run", "/api/curator/status", "/api/curator/paused", "/api/curator/pin", "/api/curator/restore",
                "/api/providers",
                "/api/memory", "/api/memory/switch",
                "/api/events/context", "/api/events/extract", "/api/events/resonance",
                "/api/mood", "/api/mood/context",
                "/api/mcp/servers", "/api/mcp/execute",
                "/api/screen/consent-status", "/api/screen/observe", "/api/debug/chat",
            ],
        })

    return app


# ── Chat plumbing ───────────────────────────────────────────────────────────


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")


# ── Phase 2: Provider-based chat ──────────────────────────────────────────

# A tool loop has NO total iteration/duration cap: the pet may keep calling
# tools for hours as long as it actually SPEAKS between rounds
# (工具→说话→工具→说话). Only a completely silent tool STREAK is cut off —
# consecutive tool calls with no user-visible reply text in between.
# The streak clock starts when the FIRST tool of the streak is about to run,
# NOT at turn start: slow prefill / long thinking before any tool call is
# the model working, not the pet silently tool-calling.
# Thinking (think events), tool status asides (_执行 recall..._), control
# tags and <<<MEM>>> extraction blocks are pipeline chatter, not speech.
SILENT_TOOL_TIMEOUT_SECONDS = 600.0


def _silent_tool_timeout_seconds() -> float:
    """Silent tool-loop budget; ``PET_SILENT_TOOL_TIMEOUT`` overrides (s)."""
    raw = os.environ.get("PET_SILENT_TOOL_TIMEOUT")
    if raw is None or not str(raw).strip():
        return SILENT_TOOL_TIMEOUT_SECONDS
    try:
        value = float(str(raw).strip())
    except ValueError:
        value = 0.0
    if value <= 0:
        get_logger().warning(
            "invalid PET_SILENT_TOOL_TIMEOUT=%r; using default %.0fs",
            raw,
            SILENT_TOOL_TIMEOUT_SECONDS,
        )
        return SILENT_TOOL_TIMEOUT_SECONDS
    return value


def silent_tool_timed_out(started_at: float, now: float, budget: float) -> bool:
    """True when the silent tool streak has lasted for the whole budget.

    ``started_at`` is the tool-streak start (first tool about to execute),
    never the turn start — a model that only thinks/prefills is not a
    silent tool streak and must not be cut off.
    """
    try:
        return (float(now) - float(started_at)) >= float(budget)
    except (TypeError, ValueError):
        return False


def _monotonic() -> float:
    """Indirection so tests can freeze/advance the tool-loop clock."""
    return time.monotonic()


# After this many recall calls in one turn, nudge the model to answer with
# what it already has. This is a SOFT hint only — it never truncates the
# loop; the silent-tool timeout above is the only hard stop.
RECALL_SOFT_LIMIT = 3
RECALL_LIMIT_NOTICE = (
    "[系统] 这一轮你已经回忆了 3 次。先基于现在记得的部分回答用户，"
    "不要再调用 recall 了；想不起来的部分就如实说没想起来。"
)
# Last-resort visible text when the loop stopped (silent-tool timeout or a
# pipeline-chatter-only ending) and even the forced tools=None answer came
# back empty. The user must never watch the bubble stream tool statuses and
# then vanish.
TOOL_LOOP_FALLBACK_TEXT = (
    "我翻了一会儿记忆，但这次没整理出答案。再问我一次，或者给我一个关键词。"
)

# Emotion tag the LLM appends to the end of its reply. Parsed by the
# gateway and pushed to the pet via ClawdBridge so the pet can pick an
# emotion-tagged animation. Never shown to the user (renderer strips it).
_EMOTION_RE = re.compile(r"\[EMOTION:\s*([a-z_]+)\]", re.IGNORECASE)
# A model sometimes echoes its tool call inline before the actual answer
# ("[MCP:builtin/capture_screen:{}]你屏幕上…"). The line-based narration
# filter only drops whole marker lines; strip an inline marker here so it
# never reaches the chat bubble.
_INLINE_TOOL_MARKER_RE = re.compile(
    r"\[\s*(?:MCP:[^\]]*|skill:[^\]]*|(?:builtin|pet(?:-world|-intuition)?)/[^\]]*)\]\s*",
    re.IGNORECASE,
)
# Matches the memory block's live now-anchor line ("（现在是2026年…。").
_NOW_ANCHOR_RE = re.compile(r"（现在是\s*[^）]*。")


def _visible_reply_text(text: str) -> str:
    """Return only the part of a provider reply a user could actually see.

    Tool-loop bookkeeping (MCP markers, ``_执行 recall..._`` asides) and
    control tags (emotion / next_chat / walk / inner / MEM block) are
    pipeline chatter. They must not count as a final answer, otherwise the
    loop thinks the pet already spoke and ends on an empty bubble.
    """
    if not text:
        return ""
    # The hidden DSTATE block (and an unfinished one) is never visible.
    text = _cortex.strip_dstate_blocks(text)
    # API reasoning and inline <think> blocks are not speech or inner voice.
    text = re.sub(r"<think>[\s\S]*?(?:</think>|$)", "", text, flags=re.IGNORECASE)
    # An unclosed MEM block still hides everything after its opener.
    s = re.sub(
        r"<<<MEM>>>[\s\S]*?(?:<<<MEMEND>>>|$)",
        "",
        text,
        flags=re.IGNORECASE,
    )
    # A MEM block split across tool rounds can leave a lone opener/closer.
    # A tag is control data, never speech, so drop any stray marker.
    s = re.sub(r"<<<(?:MEM|MEMEND)>>>", "", s, flags=re.IGNORECASE)
    # Hallucinated native tool markup is never speech: real calls arrive via
    # the tool_calls channel or [MCP:] text markers, not <tool_call> tags.
    s = re.sub(r"<tool_call>[\s\S]*?(?:</tool_call>|$)", "", s, flags=re.IGNORECASE)
    s = re.sub(r"</?tool_call>", "", s, flags=re.IGNORECASE)
    s = re.sub(
        r"\[\s*(?:EMOTION|NEXT_CHAT|WALK_DESKTOP|WALK|TURN|INNER)\s*[^\]]*\]",
        "",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(r"\[\s*(?:INNER|WALK|TURN|EMOTION|NEXT_CHAT)\s*:[^\]]*$", "", s, flags=re.IGNORECASE)
    s = _INLINE_TOOL_MARKER_RE.sub("", s)
    kept: list[str] = []
    for line in s.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith(("[MCP:", "[builtin/", "[skill:")):
            continue
        if re.match(
            r"^_{0,2}\s*执行\s+[A-Za-z_]\w*\s*(?:\.\.\.)?\s*_{0,2}$", stripped
        ):
            continue
        kept.append(stripped)
    return "\n".join(kept).strip()


def _sanitize(s: str) -> str:
    """Sanitize for tool name prefix (must match _sanitize_name in providers/base.py)."""
    return re.sub(r"[^A-Za-z0-9_]", "_", str(s or ""))


def _engine_enabled_path() -> Path:
    """Where the user's engine on/off intent is persisted."""
    root = os.environ.get("PET_MEMORY_DIR") or str(Path.home() / ".pet")
    return Path(root) / "engine-enabled.json"


def _engine_enabled() -> bool:
    """May the sidecar start the built-in engine on its own?

    Written by /api/engine/stop (False) and /api/engine/start (True).
    A missing file means True so existing installs behave as before.
    """
    try:
        p = _engine_enabled_path()
        if not p.exists():
            return True
        return bool(json.loads(p.read_text(encoding="utf-8")).get("enabled", True))
    except Exception:
        return True


def _set_engine_enabled(flag: bool) -> None:
    try:
        p = _engine_enabled_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"enabled": bool(flag)}), encoding="utf-8")
    except Exception as exc:
        get_logger().warning("could not persist engine enabled=%s: %s", flag, exc)

def _build_local_provider(server, server_state, req, lora_arr=None):
    """Build a fresh LocalProvider with the pre-computed LoRA array.

    `lora_arr` comes from `_lora_arr_for(req)`, which is the single source of
    truth: it honors `disable_adapter` and returns `[]` (not None) when no
    adapter is active, so llama-server forces base output for this request.
    Recomputing it here used to silently ignore `disable_adapter`, letting a
    persona LoRA bleed into narrator replies.
    """
    from .providers.local import LocalProvider
    return LocalProvider(
        server,
        enable_thinking=bool(req.thinking),
        lora=lora_arr,
    )


def _resolve_provider(
    registry: ProviderRegistry,
    req: ChatRequest,
    server: LlamaServer,
    server_state: dict,
    log,
    lora_arr: Optional[list[dict]] = None,
) -> BaseProvider:
    """Pick the provider for this request based on model_provider.

    Falls back to local only if explicitly requested or no API provider configured.
    Logs a clear message about which provider is being used.
    """
    from .providers.local import LocalProvider
    target = str(req.model_provider or "").strip().lower() if req.model_provider else ""

    if target and target != "local":
        if target in registry:
            log.info("provider routing: using %s (model=%s)", target, getattr(registry.get(target), '_model', '?'))
            return registry.get(target)
        # Not found - log error and fall back with warning in SSE
        names = [p.name for p in registry._providers.values()]
        log.warning("provider '%s' not found in registry (available: %s), falling back", target, names)
        # Fall through to the local/API fallback below so the chat still works

    # Default path: local — but if the local llama-server isn't running
    # (no model downloaded yet, or no llama-server binary at all), fall
    # back to any configured API provider so the pet works out of the box
    # in "API mode". Users should not need a local model to chat.
    api_names = [p.name for p in registry._providers.values() if p.name != "local"]
    if not getattr(server, "alive", False) and api_names:
        log.info("local llama-server not running; falling back to API provider '%s'", api_names[0])
        return registry.get(api_names[0])

    return _build_local_provider(server, server_state, req, lora_arr)


# ── 桌面刚体（前瞻 02 · P1）：AI 操作推 / 拉 / 拿 / 踢 ──────────────────
# 走 Electron 的 state server（127.0.0.1:23333-23337，x-clawd-token 鉴权）。
_CLAWD_PORTS = [23333, 23334, 23335, 23336, 23337]
_CLAWD_RUNTIME = Path.home() / ".clawd" / "runtime.json"
_CLAWD_TOKEN = Path.home() / ".clawd" / "server-token"


def _clawd_port() -> int:
    try:
        data = json.loads(_CLAWD_RUNTIME.read_text("utf-8"))
        port = int(data.get("port") or 0)
        if port in _CLAWD_PORTS:
            return port
    except Exception:
        pass
    return _CLAWD_PORTS[0]


def _clawd_headers() -> dict:
    try:
        token = _CLAWD_TOKEN.read_text("utf-8").strip()
    except Exception:
        token = ""
    return {"Content-Type": "application/json", "x-clawd-token": token}


async def _pet_action(kind: str, **payload) -> dict:
    """Execute one body action; only an Electron confirmation means completion."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await asyncio.wait_for(
                client.post(
                    f"http://127.0.0.1:{_clawd_port()}/pet/act",
                    json={"kind": kind, **payload},
                    headers=_clawd_headers(),
                    timeout=8.0,
                ),
                timeout=8.0,
            )
            data = resp.json()
        except Exception as exc:
            return {"status": "error", "applied": False, "message": str(exc)}
    return data if isinstance(data, dict) else {"status": "error", "applied": False, "message": "invalid body response"}


def _remember_body_action(kind: str, result: dict) -> None:
    if result.get("status") != "ok" or result.get("applied") is not True:
        return
    # Attention-density receipt: every applied body/tool event is a cheap
    # "the world moved" signal for the tempo temperature rules.
    try:
        _DENSITY_EVENTS.append((time.time(), str(kind or "")))
        if len(_DENSITY_EVENTS) > 64:
            del _DENSITY_EVENTS[:-64]
    except Exception:
        pass
    try:
        if _body_log_ref is not None:
            detail = (
                f"-> ({result.get('x')}, {result.get('y')})"
                if kind == "walk" else result.get("facing", "")
            )
            _body_log_ref.append(kind, detail, source="applied")
    except Exception:
        pass


async def _pet_view_for_capture() -> Optional[dict]:
    """Best-effort /world/bodies geometry for a cone-shaped screenshot.

    Returns {"pet", "facing", "wa"} (all DIP) or None. Short timeout: a
    slow Electron must never stall the screenshot tool.
    """
    try:
        async with httpx.AsyncClient(timeout=1.2) as client:
            resp = await client.get(
                "http://127.0.0.1:%d/world/bodies" % _clawd_port(),
                headers=_clawd_headers(),
            )
        data = resp.json()
        if data.get("status") == "error":
            return None
        pet = data.get("pet") or {}
        wa = data.get("wa") or {}
        if not pet or not wa:
            return None
        return {"pet": pet, "facing": data.get("facing") or "right", "wa": wa}
    except Exception:
        return None


def crop_to_vision_cone(
    b64: str, pet: dict, facing: str, wa: dict
) -> Optional[str]:
    """Crop a screenshot to the pet's existing vision cone.

    ``pet`` and ``wa`` are DIP geometry from /world/bodies; the screenshot
    is the primary display, so physical scale = image.width / wa.width.
    Everything outside the FOV sector is blacked out. Returns base64 JPEG
    (max side 768) or None when the geometry is unusable — the caller then
    falls back to the full screen.
    """
    from .decision.vision import FOV_HALF_DEG, FOV_RANGE_PX

    try:
        import io as _io
        from PIL import Image as _Image, ImageDraw as _ImageDraw

        wa_width = float(wa["width"])
        pet_x, pet_y = float(pet["x"]), float(pet["y"])
        pet_w, pet_h = float(pet["width"]), float(pet["height"])
        if wa_width <= 0 or pet_w <= 0 or pet_h <= 0:
            return None

        img = _Image.open(_io.BytesIO(base64.b64decode(b64))).convert("RGB")
        scale = img.width / wa_width
        if scale <= 0:
            return None
        eye_x = (pet_x + pet_w / 2) * scale
        eye_y = (pet_y + pet_h / 2) * scale
        if not (0 <= eye_x < img.width and 0 <= eye_y < img.height):
            return None

        # Extend the sector beyond the screenshot so the cone reaches the
        # display edge instead of stopping at a fixed 400px radius.
        radius = max(img.width, img.height) * 2.0
        center = 180.0 if str(facing or "").lower() == "left" else 0.0
        points = [(eye_x, eye_y)]
        for i in range(25):
            angle = math.radians(center - FOV_HALF_DEG + 2 * FOV_HALF_DEG * i / 24)
            points.append((
                round(eye_x + math.cos(angle) * radius, 3),
                round(eye_y + math.sin(angle) * radius, 3),
            ))

        left = max(0, int(math.floor(min(p[0] for p in points))))
        top = max(0, int(math.floor(min(p[1] for p in points))))
        right = min(img.width, int(math.ceil(max(p[0] for p in points))))
        bottom = min(img.height, int(math.ceil(max(p[1] for p in points))))
        if right <= left or bottom <= top:
            return None

        crop = img.crop((left, top, right, bottom))
        mask = _Image.new("L", crop.size, 0)
        _ImageDraw.Draw(mask).polygon(
            [(x - left, y - top) for x, y in points], fill=255
        )
        out = _Image.composite(crop, _Image.new("RGB", crop.size, (0, 0, 0)), mask)

        max_side = 768
        if max(out.size) > max_side:
            ratio = max_side / max(out.size)
            out = out.resize((int(out.size[0] * ratio), int(out.size[1] * ratio)), _Image.LANCZOS)
        buf = _io.BytesIO()
        out.save(buf, format="JPEG", quality=70)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


# ── 刚体选择：走到自己选中的刚体旁边 / 站上去 ──────────────────────────
# 模型只负责「选哪个」（id）；位移算术由网关做好交给壳的 /pet/walk 执行
# （walkPet 用 DIP 并 clamp 到工作区，与 /world/bodies 的 rect 同单位）。
_WALK_BESIDE_GAP = 28.0  # 停在刚体侧边的留白（DIP）
_LAST_WORLD_WA: dict = {"rect": None}  # scout 最近一次拿到的工作区


def _walk_to_delta(pet: dict, body: dict, stand: bool = False, gap: float = _WALK_BESIDE_GAP) -> tuple[float, float] | None:
    """Compute the (dx, dy) DIP delta that walks the pet to a chosen body.

    ``pet``: {x, y, width, height} window bounds; ``body``: {rect:{left,
    top, right, bottom}}. Beside-mode stops next to the body's near edge on
    the pet's current side, vertically aligned to the body's ground line;
    stand-mode centers the pet over the body top (physics settles it).
    Returns None when the geometry is unusable.
    """
    try:
        pw, ph = float(pet["width"]), float(pet["height"])
        pcx = float(pet["x"]) + pw / 2
        pcy = float(pet["y"]) + ph / 2
        r = body["rect"]
        left, top = float(r["left"]), float(r["top"])
        right, bottom = float(r["right"]), float(r["bottom"])
    except (KeyError, TypeError, ValueError):
        return None
    bcx = (left + right) / 2
    if stand:
        tx = bcx
        ty = top - ph / 2  # pet bottom lands on the body's top surface
    else:
        side = -1 if pcx < bcx else 1  # approach from wherever the pet already is
        tx = (left - gap - pw / 2) if side < 0 else (right + gap + pw / 2)
        ty = bottom - ph / 2  # stand on the body's ground line
    return (tx - pcx, ty - pcy)


def _window_view(pet: dict, rect: dict, facing: str = "right") -> dict:
    """The pet's OWN perspective on a desktop window: nearest-edge distance
    + bearing (relative to facing), and whether any part of it is inside the
    vision cone. Pure math so the scout tool's view matches「只看见看得见的」.
    """
    try:
        pcx = float(pet["x"]) + float(pet["width"]) / 2
        pcy = float(pet["y"]) + float(pet["height"]) / 2
        left, top = float(rect["left"]), float(rect["top"])
        right, bottom = float(rect["right"]), float(rect["bottom"])
    except (KeyError, TypeError, ValueError):
        return {"dist": 9999.0, "angle": 0.0, "in_view": False}
    # nearest point of the rect to the pet's center (windows are big; the
    # pet "sees" a window when any part of it is close and in front)
    nx = min(max(pcx, left), right)
    ny = min(max(pcy, top), bottom)
    dx, dy = nx - pcx, ny - pcy
    dist = math.hypot(dx, dy)
    from .decision.vision import visible
    vis = visible(dx, dy, facing) if dx or dy else {"angle": 0.0, "in_fov": True}
    return {"dist": round(dist, 1), "angle": float(vis.get("angle", 0.0)), "in_view": bool(vis.get("in_fov"))}


def _format_windows_scout(windows: list, pet: dict, facing: str) -> str:
    """Candidate-window listing from the pet's own viewpoint, for it to pick
    one to ADOPT as a rigid body (pet_world_scout → pet_world_adopt)."""
    from .decision.vision import label_for_angle
    if not windows:
        return "桌面上没有可以收养的窗户。"
    for w in windows:
        w["_view"] = _window_view(pet, w.get("rect") or {}, facing)
    windows.sort(key=lambda w: (0 if w["_view"]["in_view"] else 1, w["_view"]["dist"]))
    lines = ["系统返回这些窗户的几何位置（不是屏幕图像；可以自己挑一扇收养成刚体）："]
    wa = None
    try:
        wa = _LAST_WORLD_WA.get("rect")
    except Exception:
        pass
    for w in windows[:12]:
        v = w["_view"]
        r = w.get("rect") or {}
        maximized = False
        if wa:
            maximized = (float(r.get("top", 0)) <= float(wa.get("y", 0)) + 1
                         and float(r.get("right", 0)) - float(r.get("left", 0)) >= float(wa.get("width", 1e9)) * 0.95)
        lines.append(
            "- hwnd=%s 「%s」 %s %dpx%s%s%s"
            % (
                w.get("hwnd"),
                (w.get("title") or "").strip() or "（无标题）",
                label_for_angle(v["angle"]),
                int(v["dist"]),
                "（已是刚体）" if w.get("already") else "",
                "（最大化，站不上去）" if maximized else "",
                "" if v["in_view"] else "（视野边缘之外）",
            )
        )
    lines.append("用 pet_world_adopt 收养你选中的那扇（传它的 hwnd）。")
    return "\n".join(lines)


def _format_bodies_choice(items: list, facing: str) -> str:
    """Vision-aware rigid-body listing for the model to CHOOSE from.

    Only what the pet can actually see (视锥 ±30°/400px, same rule the
    subconscious uses); the rest is summarized as a count so the model
    never picks a body it cannot perceive.
    """
    from .decision.vision import label_for_angle
    vis = sorted(
        (b for b in items if b.get("in_fov")),
        key=lambda b: float(b.get("dist") or 1e9),
    )
    hidden = len(items) - len(vis)
    if not vis:
        if hidden:
            return "视野内没有刚体（还有 %d 个在视野外——走近或转身才能看到）。" % hidden
        return "现在桌面上还没有刚体（用户还没添加窗口/桌面图标）。"
    lines = ["视野内的刚体（按远近，可以自己挑）："]
    for b in vis:
        lines.append(
            "- id=%s 「%s」 %s %dpx"
            % (
                b.get("id"),
                b.get("label") or "刚体",
                label_for_angle(float(b.get("angle") or 0)),
                int(float(b.get("dist") or 0)),
            )
        )
    if hidden:
        lines.append("（另有 %d 个在视野外，你看不见。）" % hidden)
    return "\n".join(lines)


async def _world_tool(name: str, args: dict) -> str:
    """执行桌面刚体工具（pet_world_bodies / pet_world_walk_to / pet_world_operate）。"""
    port = _clawd_port()
    headers = _clawd_headers()

    def reply(summary: str, status: str = "error", applied: bool = False) -> str:
        return json.dumps({"status": status, "applied": applied, "summary": summary}, ensure_ascii=False)

    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            if name == "pet_world_bodies":
                # 视锥内的选择列表（与潜意识同一条可见性规则）：
                # 模型只能从它看得见的刚体里挑。
                items = await _world_bodies_for_decision(force=True)
                facing = str(_WORLD_VIEW_CACHE.get("facing") or "right")
                return _format_bodies_choice(items, facing)
            if name == "pet_world_scout":
                resp = await client.get("http://127.0.0.1:%d/world/windows" % port, headers=headers)
                data = resp.json()
                if data.get("status") == "error":
                    return reply("看不了窗户：%s" % (data.get("message") or "unknown"))
                _LAST_WORLD_WA["rect"] = data.get("wa")
                return _format_windows_scout(
                    data.get("windows") or [],
                    data.get("pet") or {},
                    str(data.get("facing") or "right"),
                )
            if name == "pet_world_adopt":
                hwnd = str(args.get("hwnd") or "").strip()
                if not hwnd:
                    return reply("要传 hwnd（从 pet_world_scout 里选）。")
                resp = await client.post(
                    "http://127.0.0.1:%d/world/add" % port,
                    json={"hwnd": hwnd},
                    headers=headers,
                )
                data = resp.json()
                if data.get("status") == "ok" and data.get("applied", True) is True:
                    b = data.get("body") or {}
                    try:
                        if _body_log_ref is not None:
                            _body_log_ref.append(
                                "adopt", "hwnd=%s label=%s" % (hwnd, b.get("label") or "?"), source="applied"
                            )
                    except Exception:
                        pass
                    return reply("收养成功：「%s」现在是刚体 id=%s，可以走过去或站上去了。" % (
                        b.get("label") or "窗户", b.get("id"),
                    ), "ok", True)
                _msg = str(data.get("message") or "unknown")
                if "already" in _msg:
                    return reply("这扇窗户已经是刚体了。", "not_applied")
                return reply("[world] 收养失败：%s" % _msg,
                             "not_applied" if data.get("status") == "ok" else "error")
            if name == "pet_world_walk_to":
                body_id = int(args.get("id") or 0)
                stand = bool(args.get("stand"))
                resp = await client.get("http://127.0.0.1:%d/world/bodies" % port, headers=headers)
                data = resp.json()
                if data.get("status") == "error":
                    return reply("桌面物理现在不可用：%s" % (data.get("message") or "unknown"))
                pet = data.get("pet") or {}
                chosen = next(
                    (b for b in (data.get("bodies") or []) if int(b.get("id") or -1) == body_id),
                    None,
                )
                if chosen is None:
                    return reply("没有 id=%s 的刚体（先 pet_world_bodies 看看有哪些）。" % body_id)
                if not pet:
                    return reply("拿不到自己的位置，走不动。")
                if stand:
                    # 最大化窗户顶边在屏幕外：物理上站不上去（会被物理引擎
                    # 弹回地面），诚实拒绝而不是静默钳制。
                    try:
                        wa_top = float((data.get("wa") or {}).get("y", 0))
                        ph = float(pet.get("height", 0))
                        if float(chosen["rect"]["top"]) - ph / 2 < wa_top:
                            return reply("「%s」是铺满屏幕的最大化窗户，顶边在屏幕外，站不上去——走到旁边（stand=false）或者换一扇吧。" % (chosen.get("label") or "刚体"), "not_applied")
                    except (KeyError, TypeError, ValueError):
                        pass
                delta = _walk_to_delta(pet, chosen, stand=stand)
                if delta is None:
                    return reply("这个刚体的几何信息不可用。")
                dx, dy = (round(delta[0]), round(delta[1]))
                if dx == 0 and dy == 0:
                    return reply("已经在「%s」%s了。" % (chosen.get("label") or "刚体", "上面" if stand else "旁边"), "not_applied")
                wresp = await client.post(
                    "http://127.0.0.1:%d/pet/act" % port,
                    json={"kind": "walk", "dx": dx, "dy": dy},
                    headers=headers,
                    timeout=8.0,
                )
                wdata = wresp.json()
                if wdata.get("status") == "ok" and wdata.get("applied") is True:
                    _remember_body_action("walk", wdata)
                    return json.dumps(dict(wdata, summary="走到了「%s」%s（现在的位置 x=%s, y=%s）。" % (
                        chosen.get("label") or "刚体", "上面" if stand else "旁边",
                        wdata.get("x"), wdata.get("y"),
                    )), ensure_ascii=False)
                return reply("[world] 走不过去：%s" % (wdata.get("message") or "unknown"),
                             "error" if wdata.get("status") == "error" else "not_applied")
            if name == "pet_world_operate":
                body_id = int(args.get("id") or 0)
                action = str(args.get("action") or "")
                payload = {"id": body_id, "action": action}
                if args.get("distance") is not None:
                    payload["distance"] = args.get("distance")
                resp = await client.post(
                    "http://127.0.0.1:%d/world/op" % port, json=payload, headers=headers
                )
                data = resp.json()
                if data.get("status") == "ok" and data.get("applied", True) is True:
                    try:
                        if _body_log_ref is not None:
                            _body_log_ref.append(action, "id=%s" % body_id, source="applied")
                    except Exception:
                        pass
                    to = data.get("to") or {}
                    return reply("动作 %s 完成，刚体移动到 (x=%s, y=%s)" % (
                        action, round(to.get("x", 0)), round(to.get("y", 0)),
                    ), "ok", True)
                _msg = str(data.get("message") or "unknown")
                if "physics is not running" in _msg or "unavailable" in _msg:
                    return reply("桌面物理现在不可用：%s" % _msg)
                return reply("[world] 操作失败：%s" % _msg,
                             "not_applied" if data.get("status") == "ok" else "error")
    except Exception as exc:
        return reply("[world] 连接桌宠失败：%s" % exc)
    return reply("[world] 未知工具 %s" % name)


_WORLD_VIEW_CACHE: dict = {"t": 0.0, "bodies": [], "facing": "right"}
_WORLD_VIEW_TTL = 2.0


async def _world_bodies_for_decision(force: bool = False) -> list[dict]:
    """Bodies + distance/bearing to the pet, cached for 2s.

    Best-effort: the decision loop must never block on Electron. On
    timeout/error return [] — the space intuition then contributes nothing.
    """
    now = time.time()
    if not force and now - float(_WORLD_VIEW_CACHE["t"]) < _WORLD_VIEW_TTL:
        return list(_WORLD_VIEW_CACHE["bodies"])
    try:
        port = _clawd_port()
        async with httpx.AsyncClient(timeout=0.8) as client:
            resp = await client.get(
                "http://127.0.0.1:%d/world/bodies" % port,
                headers=_clawd_headers(),
            )
            data = resp.json()
        if data.get("status") == "error":
            _WORLD_VIEW_CACHE.update({"t": now, "bodies": [], "facing": "right"})
            return []
        from .decision.vision import visible
        facing = data.get("facing") or "right"
        pet = data.get("pet") or {}
        px = py = None
        if pet:
            px = float(pet.get("x", 0)) + float(pet.get("width", 0)) / 2
            py = float(pet.get("y", 0)) + float(pet.get("height", 0)) / 2
        out = []
        for b in data.get("bodies") or []:
            r = b.get("rect") or {}
            left, top = float(r.get("left", 0)), float(r.get("top", 0))
            right, bottom = float(r.get("right", 0)), float(r.get("bottom", 0))
            item = {
                "id": b.get("id"),
                "label": b.get("label") or "",
                "dist": 9999.0,
                "angle": 0.0,
                "in_fov": False,
            }
            if px is not None and py is not None:
                cx, cy = (left + right) / 2, (top + bottom) / 2
                item.update(visible(cx - px, cy - py, facing))
            out.append(item)
        _WORLD_VIEW_CACHE.update({"t": now, "bodies": out, "facing": facing})
        return out
    except Exception:
        _WORLD_VIEW_CACHE.update({"t": now, "bodies": [], "facing": "right"})
        return []


def _format_vision_context() -> str:
    """Only what the pet can actually see — not the whole desktop."""
    bodies = _WORLD_VIEW_CACHE.get("bodies") or []
    visible_bodies = [b for b in bodies if b.get("in_fov")]
    if not visible_bodies:
        return ""
    from .decision.vision import label_for_angle
    facing = _WORLD_VIEW_CACHE.get("facing") or "right"
    lines = ["【你的视野】你现在朝%s看。看得见：" % ("右" if facing == "right" else "左")]
    for b in visible_bodies[:5]:
        lines.append("- %s：%s %dpx" % (
            b.get("label") or "刚体",
            label_for_angle(float(b.get("angle") or 0)),
            int(float(b.get("dist") or 0)),
        ))
    lines.append("（视野外的东西你看不见；想看清可以走近或转头。）")
    return "\n".join(lines)


def _evolve_json(payload: Any, limit: int = 12000) -> str:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if len(text) > limit:
        return text[:limit] + "\n…（已截断）"
    return text


async def _evolve_tool(name: str, args: dict) -> str:
    """Built-in self-evolution tools.

    The service is synchronous (git + subprocess); ``to_thread`` keeps the
    streaming event loop responsive while a candidate test suite runs.
    """
    svc = _evolve_service()
    if svc is None:
        return "[evolve] 自我进化工作台没有启动。"
    from .evolve.models import EvolveError

    args = args if isinstance(args, dict) else {}
    try:
        if name == "pet_evolve_inspect":
            return await asyncio.to_thread(svc.inspect)
        if name == "pet_evolve_read":
            path = str(args.get("path") or "").strip()
            if not path:
                return "[evolve] 需要 path（仓库相对路径）。"
            try:
                start = max(1, int(args.get("start") or 1))
            except (TypeError, ValueError):
                start = 1
            end = args.get("end")
            if end is not None:
                try:
                    end = max(start, int(end))
                except (TypeError, ValueError):
                    end = None
            return await asyncio.to_thread(
                svc.read_source, path, start=start, end=end
            )
        if name == "pet_evolve_propose":
            goal = str(args.get("goal") or "").strip()
            plan = str(args.get("plan") or "").strip()
            edits = args.get("edits")
            if not goal or not plan or not isinstance(edits, list):
                return "[evolve] 需要 goal、plan 和 edits=[{path,old,new}]。"
            rec = await asyncio.to_thread(
                svc.propose,
                goal=goal,
                plan=plan,
                edits=edits,
                kind=str(args.get("kind") or "core"),
                title=str(args.get("title") or ""),
            )
            return _evolve_json(
                {
                    "candidate_id": rec.get("id"),
                    "kind": rec.get("kind"),
                    "status": rec.get("status"),
                    "files_changed": rec.get("files_changed"),
                    "diff_sha256": rec.get("diff_sha256"),
                    "base_dirty": rec.get("base_dirty"),
                    "next": "调用 pet_evolve_evaluate 下载/运行测试；核心改动在 M1 只审阅。",
                }
            )
        if name == "pet_evolve_evaluate":
            cid = str(args.get("candidate_id") or "").strip()
            if not cid:
                return "[evolve] 需要 candidate_id。"
            result = await asyncio.to_thread(
                svc.evaluate, cid, full=bool(args.get("full"))
            )
            return _evolve_json(result)
        if name == "pet_evolve_list":
            try:
                limit = max(1, min(50, int(args.get("limit") or 20)))
            except (TypeError, ValueError):
                limit = 20
            return _evolve_json(await asyncio.to_thread(svc.list_candidates, limit))
        if name == "pet_evolve_show":
            cid = str(args.get("candidate_id") or "").strip()
            if not cid:
                return "[evolve] 需要 candidate_id。"
            rec = await asyncio.to_thread(svc.show, cid)
            view = {
                key: rec.get(key)
                for key in (
                    "id",
                    "kind",
                    "title",
                    "goal",
                    "plan",
                    "status",
                    "eval_status",
                    "verified",
                    "base_commit",
                    "base_dirty",
                    "files_changed",
                    "diff_sha256",
                    "plugin_name",
                    "promotion_id",
                    "created_at",
                    "eval",
                )
            }
            try:
                diff_path = svc.store.candidate_dir(cid) / "diff.patch"
                if diff_path.is_file():
                    diff = diff_path.read_text(encoding="utf-8")
                    view["diff"] = diff[-8000:]
            except Exception:
                pass
            return _evolve_json(view)
        if name == "pet_evolve_discard":
            cid = str(args.get("candidate_id") or "").strip()
            if not cid:
                return "[evolve] 需要 candidate_id。"
            rec = await asyncio.to_thread(svc.discard, cid)
            return _evolve_json({"candidate_id": rec.get("id"), "status": rec.get("status")})
        return f"[evolve] 未知工具: {name}"
    except EvolveError as exc:
        return f"[evolve] {exc}"
    except Exception as exc:
        _note_failure("evolve_tool", exc)
        return f"[evolve error: {type(exc).__name__}: {exc}]"


def _tool_feedback_text(result: Any, summary: str, limit: int = 6000) -> str:
    """The payload a text-only/native model should see after a tool call.

    ``memory list`` puts the actual tree in ``content`` while its one-line
    ``summary`` is only the usage note. Feeding back the summary alone made
    the web model say "目录回来了，但里面没列出具体文件" and then guess
    paths. Prefer the real content when it is small and text-only; fall back
    to the human summary for oversized/binary results.
    """
    try:
        if isinstance(result, dict):
            content = result.get("content")
            if isinstance(content, list):
                texts = []
                for part in content:
                    if not isinstance(part, dict) or part.get("type") != "text":
                        continue
                    text = part.get("text")
                    if text:
                        texts.append(str(text))
                text = "\n".join(texts).strip()
                if text and len(text) <= limit and "base64," not in text[:200]:
                    return text
    except Exception:
        pass
    return summary


def _intuition_tool(name: str, args: dict) -> str:
    """Built-in tools for the model to grow/prune its own intuitions."""
    reg = _intuition_registry_ref
    if reg is None:
        return "[intuition] 决策层没有启动，暂时不能改直觉。"
    if name == "pet_list_intuitions":
        items = reg.describe()
        lines = ["当前直觉（内置不可删）："]
        for it in items:
            lines.append(
                "- %s（%s，p_emit=%s，weight=%s%s）：%s" % (
                    it["name"], it["kind"], it["p_emit"], it["weight"],
                    "，内置" if it["builtin"] else "", it["description"],
                )
            )
        return "\n".join(lines)
    if name == "pet_add_intuition":
        try:
            p_emit = float(args.get("p_emit") if args.get("p_emit") is not None else 0.1)
            weight = float(args.get("weight") if args.get("weight") is not None else 1.0)
        except Exception:
            return "[intuition] p_emit/weight 必须是数字。"
        res = reg.add_custom(
            name=str(args.get("name") or ""),
            description=str(args.get("description") or ""),
            code=str(args.get("code") or ""),
            kind=str(args.get("kind") or "custom"),
            p_emit=p_emit,
            weight=weight,
        )
        if res.get("status") != "ok":
            return "[intuition] 注册失败：%s" % res.get("message")
        try:
            if _ephemeral_ref is not None and _ephemeral_ref.memory_dir:
                reg.save_custom(Path(_ephemeral_ref.memory_dir) / "intuitions.json")
        except Exception as exc:
            return "[intuition] 已注册，但保存失败：%s" % exc
        return "直觉 %s 已注册。下一次决策循环就会把它纳入。" % res.get("name")
    if name == "pet_remove_intuition":
        target = str(args.get("name") or "").strip()
        res = reg.remove(target)
        if res.get("status") != "ok":
            return "[intuition] 删除失败：%s" % res.get("message")
        return "已删除直觉 %s。" % target
    return "[intuition] 未知工具 %s" % name


async def _decision_roll_tool(args: dict) -> dict:
    """Builtin pet_decision_roll: one Laya-assisted roll over model options.

    The model proposes 2..6 options with gut weights; Laya supplies
    probabilities; the sampler blends them and draws from the top-3
    posterior. ``user_text`` comes from the turn ContextVar so the roll sees
    what the user actually said (never a narration prompt).
    """
    args = args if isinstance(args, dict) else {}
    used = int(_active_roll_count_ref.get() or 0)
    if used >= _MAX_DECISION_ROLLS_PER_TURN:
        return {
            "content": [{"type": "text", "text": (
                "这一轮已经掷过 3 次骰了。别继续掷——直接选一个，或者直接回答。"
            )}],
            "is_error": True,
            "summary": "Roll limit reached (3/turn); commit to a choice",
        }

    roll_kwargs: dict = {
        "context": str(args.get("context") or ""),
        "user_text": _active_user_text(),
    }
    seed = args.get("seed")
    # bool is an int subclass; a True seed would silently roll with 1.
    if isinstance(seed, int) and not isinstance(seed, bool):
        roll_kwargs["seed"] = seed

    try:
        result = await _decision_sampler.roll_decision(
            args.get("question"), args.get("options"), **roll_kwargs
        )
    except ValueError as exc:
        return {
            "content": [{"type": "text", "text": f"掷骰参数有误：{exc}"}],
            "is_error": True,
            "summary": f"Invalid roll args: {exc}",
        }
    _active_roll_count_ref.set(used + 1)

    choice = str(result.get("choice") or "")
    confidence = float(result.get("confidence") or 0.0)
    source = str(result.get("source") or "unknown")
    summary = f"掷骰 → {choice} ({confidence:.2f}, {source})"
    laya_choice = (result.get("laya") or {}).get("choice")
    if laya_choice and laya_choice != choice:
        summary += f" laya={laya_choice}"

    telemetry.inc("decision.roll.total")
    telemetry.inc("decision.roll." + source.replace(".", "_"))
    # Name says ms: observe the sampler's own millisecond budget as-is.
    telemetry.observe("decision.roll.latency_ms", float(result.get("latency_ms") or 0.0))
    telemetry.gauge("decision.roll.confidence", confidence)

    return {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
        "is_error": False,
        "summary": summary,
    }


async def _decision_override_tool(args: dict) -> dict:
    """Builtin pet_override_decision: log a deliberate veto of a roll.

    Logging only — it never changes the current turn. The distiller learns
    from the mismatch between what was rolled and what was actually chosen.
    """
    args = args if isinstance(args, dict) else {}
    try:
        result = _decision_sampler.record_override(
            args.get("roll_id"),
            args.get("choice"),
            args.get("reason"),
        )
    except ValueError as exc:
        return {
            "content": [{"type": "text", "text": f"否决记录参数有误：{exc}"}],
            "is_error": True,
            "summary": f"Invalid override args: {exc}",
        }

    telemetry.inc("decision.override")
    return {
        "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
        "is_error": False,
        "summary": f"否决已记录 → {result.get('choice')}",
    }


async def _enter_slow_tool(args: dict) -> dict:
    """Builtin pet_enter_slow: upgrade the REST of this round's thinking.

    The provider loop rebuilds its chat kwargs from the live ``req`` every
    iteration, so mutating ``thinking`` / ``reasoning_effort`` here makes the
    next generation deeper without starting a new round.
    """
    req = _active_chat_req_ref.get()
    if req is None:
        return {
            "content": [{"type": "text", "text": "当前没有进行中的对话轮次，无法升级慢直觉。"}],
            "is_error": True,
            "summary": "No active chat turn for slow intuition",
        }
    args = args if isinstance(args, dict) else {}
    strength = str(args.get("strength") or "low").strip().lower()
    if strength not in ("low", "high", "max"):
        strength = "low"
    req.thinking = True
    req.reasoning_effort = strength
    telemetry.inc("decision.slow.enter")
    return {
        "content": [{"type": "text", "text": f"已进入慢直觉（{strength}）。接下来的思考会更深。"}],
        "is_error": False,
        "summary": f"slow intuition: {strength}",
    }


async def _decision_tool(args: dict) -> dict:
    """Builtin pet_decision: ask the same D core once, immediately.

    ``active_decision`` never mutates or saves the store, so an active gut
    check cannot overwrite the per-theme life state the passive path owns.
    """
    store = _get_dstate_store()
    if store is None:
        return {
            "content": [{"type": "text", "text": "D 还不可用。"}],
            "is_error": True,
            "summary": "D unavailable",
        }
    args = args if isinstance(args, dict) else {}
    current_state = {
        "user_text": _active_user_text(),
        "is_proactive": not _active_user_text(),
        "now": time.time(),
        "last_user": dict(store.last_user or {}),
        "last_round": dict(store.last_round or {}),
    }
    action_options = args.get("action_options")
    if not isinstance(action_options, list):
        action_options = None
    try:
        decision = await _cortex.active_decision(
            store,
            current_state,
            action_options=action_options,
            context=str(args.get("context") or ""),
            timeout=CORTEX_DECISION_TIMEOUT,
        )
        if not isinstance(decision, dict):
            raise TypeError("cortex decision is not a dict")
    except ValueError as exc:
        return {
            "content": [{"type": "text", "text": f"主动 D 参数有误：{exc}"}],
            "is_error": True,
            "summary": f"Invalid pet_decision args: {exc}",
        }
    except Exception as exc:
        telemetry.inc("decision.cortex.errors")
        return {
            "content": [{"type": "text", "text": f"主动 D 暂时不可用：{exc}"}],
            "is_error": True,
            "summary": f"pet_decision failed: {type(exc).__name__}",
        }
    telemetry.inc("decision.cortex.active")
    summary = "主动 D → %s/%s/%s" % (
        decision.get("strength"),
        decision.get("reaction"),
        decision.get("action"),
    )
    return {
        "content": [{"type": "text", "text": json.dumps(decision, ensure_ascii=False)}],
        "is_error": False,
        "summary": summary,
    }


# ── 节奏（tempo）状态机 ──────────────────────────────────────────────────────
# Three primary cadence tiers. The DEFAULT resting cadence is "slow"
# (~4–6 min between life rounds): the cheap small-model reflex layer will
# handle in-between ticks later, so slowing the main-brain cadence IS the
# token saving for now. The renderer samples the actual next-tick delay from
# the tempo SSE event; _TEMPO_SPECS here is the authoritative source for the
# interval semantics AND for the per-round temperature base.
_TEMPO_MODES = ("ultra", "fast", "slow")
_TEMPO_SPECS = {
    "ultra": {"interval_s": (50, 70), "temp": 1.3, "label": "极快反应"},
    "fast": {"interval_s": (90, 150), "temp": 1.2, "label": "快反应"},
    "slow": {"interval_s": (240, 360), "temp": 1.0, "label": "慢反应"},
}
_TEMPO_DEFAULT = "slow"
# A model/user-set tempo expires after 10 minutes and falls back to
# derivation (currently: the resting "slow" cadence).
_TEMPO_TTL_S = 600.0
# Two consecutive real user turns within this gap mean an active
# conversation → "ultra"; a lone turn is chat mode → "fast".
_TEMPO_CHAT_FAST_GAP_S = 120.0

# The server-side tempo store: a mode set by the model (pet_set_tempo) or by
# chat-mode entry survives across rounds until it expires. Empty mode or an
# expired entry ⇒ fall through to per-round derivation.
_TEMPO_STATE = {"mode": "", "expires_at": 0.0, "source": ""}


# ── Attention density (注意力密度 → 温度微调) ────────────────────────────────
# Cheap, deterministic rules (documented upgrade point: a Laya density meter
# later). "high" only counts a very recent real user turn; "medium" covers
# residual conversational context and fresh tool activity. The delta nudges
# the tempo temperature by ±0.01: high attention cools the sampling slightly,
# low attention lets the tempo base stand.
_TEMPO_DENSITY_DELTAS = {"low": 0.0, "medium": 0.01, "high": -0.01}
# Tool/body events recorded by _remember_body_action within this window
# count as fresh activity for the "medium" level.
_DENSITY_EVENT_WINDOW_S = 300.0
_DENSITY_EVENT_MEDIUM_COUNT = 2

# Module-level receipt sink: _remember_body_action appends (timestamp, kind)
# for every *applied* body/tool event; the density rules count them. Kept
# tiny and pruned on read.
_DENSITY_EVENTS: list[tuple[float, str]] = []
_DENSITY_STATE: dict = {"last_round_at": 0.0, "last_user_turn_at": 0.0}


def _density_level(now: float) -> str:
    """Attention density for this round: high / medium / low (deterministic)."""
    last_user = float(_DENSITY_STATE.get("last_user_turn_at") or 0.0)
    last_round = float(_DENSITY_STATE.get("last_round_at") or 0.0)
    if last_user > 0.0 and 0.0 <= now - last_user <= 60.0:
        return "high"
    user_5min = last_user > 0.0 and 0.0 <= now - last_user <= 300.0
    fresh_events = [
        (ts, kind) for ts, kind in _DENSITY_EVENTS
        if (last_round <= 0.0 or ts > last_round)
        and 0.0 <= now - ts <= _DENSITY_EVENT_WINDOW_S
    ]
    if user_5min or len(fresh_events) >= _DENSITY_EVENT_MEDIUM_COUNT:
        return "medium"
    return "low"


def _density_delta(now: float) -> float:
    """Temperature delta for the resolved density level (±0.01 / 0)."""
    return _TEMPO_DENSITY_DELTAS.get(_density_level(now), 0.0)


def _set_tempo(mode: str, source: str, ttl_s: float = _TEMPO_TTL_S) -> str:
    """Write a tempo into the store; unknown modes fall back to the default.

    Returns the mode actually stored (the fallback is part of the contract:
    pet_set_tempo keeps its "invalid → slow" behaviour).
    """
    mode = str(mode or "").strip().lower()
    if mode not in _TEMPO_MODES:
        mode = _TEMPO_DEFAULT
    try:
        ttl = float(ttl_s)
    except (TypeError, ValueError):
        ttl = _TEMPO_TTL_S
    if ttl <= 0:
        ttl = _TEMPO_TTL_S
    _TEMPO_STATE["mode"] = mode
    _TEMPO_STATE["expires_at"] = time.time() + ttl
    _TEMPO_STATE["source"] = str(source or "")
    return mode


def _resolve_tempo(now: float) -> tuple[str, str]:
    """One tempo decision per round → (mode, source).

    Store wins while fresh; otherwise derive. The derivation has collapsed
    to the resting cadence: idle no longer changes tempo (fatigue/rest is a
    separate battery system's job — token consumption, not wall-clock idle).
    """
    state_mode = str(_TEMPO_STATE.get("mode") or "").strip().lower()
    if state_mode in _TEMPO_MODES and float(_TEMPO_STATE.get("expires_at") or 0.0) > now:
        return state_mode, str(_TEMPO_STATE.get("source") or "store")
    return _TEMPO_DEFAULT, "derive"


def _note_user_turn_for_tempo(now: float) -> str:
    """Chat-mode entry: a real user turn switches the AI to a faster tempo.

    Two user turns within _TEMPO_CHAT_FAST_GAP_S = active conversation
    ("ultra"); otherwise plain chat mode ("fast"). The tempo expires with the
    normal TTL, so after ~10 quiet minutes the round derivation takes over
    again. Returns the mode it set.
    """
    previous = float(_TEMPO_STATE.get("last_user_turn_at") or 0.0)
    mode = (
        "ultra"
        if previous > 0.0 and 0.0 <= now - previous <= _TEMPO_CHAT_FAST_GAP_S
        else "fast"
    )
    _set_tempo(mode, "chat")
    _TEMPO_STATE["last_user_turn_at"] = now
    return mode


# ── Tool display labels（工具中文名，仅用于展示） ────────────────────────────
# Display text only: the `tool=`/meta fields in SSE events and the
# narration-strip regex contract stay the RAW tool id (machine-readable).
# Unknown tools fall back to the raw id.
_TOOL_LABELS = {
    "capture_screen": "看屏幕",
    "observe_screen": "读屏幕文字",
    "screen_click": "点击屏幕",
    "desktop_task": "桌面任务",
    "memory": "记忆",
    "recall": "回忆",
    "memory_search": "搜记忆",
    "pet_decision": "直觉判断",
    "pet_decision_roll": "掷骰",
    "pet_override_decision": "改判",
    "pet_enter_slow": "深思考",
    "pet_set_tempo": "调节奏",
    "pet_world_walk_to": "走向刚体",
    "pet_world_scout": "扫视周围",
    "pet_world_bodies": "看周围刚体",
    "pet_world_adopt": "收养刚体",
    "pet_turn_head": "转头",
    "pet_forge_plugin": "造器官",
    "pet_mood_now": "看心情",
    "pet_self_note_add": "写自我笔记",
    "pet_self_note_read": "读自我笔记",
}


def _tool_label(tool_name: str) -> str:
    """Chinese display label for a tool id; unknown tools keep the raw id."""
    return _TOOL_LABELS.get(tool_name, tool_name)


CORE_TOOL_NAMES = {
    "memory",
    "recall",
    "memory_search",
    # The decision pair is tiny (schemas are short) and is most useful on
    # exactly the cheap local models that run core-only: no long reasoning,
    # just "propose options, let Laya roll".
    "pet_decision_roll",
    "pet_override_decision",
    # Slow-intuition upgrade + an immediate active D gut check. Both are
    # first-class soul tools on every provider tier.
    "pet_enter_slow",
    "pet_decision",
    "pet_set_tempo",
    "pet_turn_head",
}


def _build_tool_map(tools: Optional[list[ToolDef]]) -> dict[str, tuple[str, str]]:
    """Map the names a model may emit to (server_name, tool_name).

    Prefixed names (``mcp_{server}_{tool}``) are always registered. Models
    also emit bare names (``pet_world_walk_to``, ``observe_screen``) — register
    those too when they are unique across the whole tool list. A duplicate
    bare name keeps only the prefixed form: routing it to an arbitrary server
    would make "walk" silently hit the wrong tool.
    """
    tool_map: dict[str, tuple[str, str]] = {}
    if not tools:
        return tool_map
    for t in tools:
        prefixed = f"mcp_{_sanitize(t.server_name)}_{_sanitize(t.name)}"
        tool_map[prefixed] = (t.server_name, t.name)
    bare_counts = Counter(t.name for t in tools)
    for t in tools:
        if bare_counts[t.name] == 1 and t.name not in tool_map:
            tool_map[t.name] = (t.server_name, t.name)
    return tool_map


async def _gather_tools(
    mcp_manager: MCPManager,
    req: ChatRequest,
    core_only: bool = False,
) -> Optional[list[ToolDef]]:
    """Gather MCP tool definitions if tools are enabled. Plugin tools
    (petplugins container — the model's self-authored organs) join the
    same tool list, prefixed pet_ to stay collision-free with MCP.

    ``core_only`` keeps only the small soul tools when a provider disables
    heavy tool schemas. Matching by tool name covers both builtin and pet
    implementations without exposing world, intuition, or other MCP tools.
    """
    if not req.tools_enabled:
        return None
    tool_defs: list[ToolDef] = []
    try:
        tools_data = await mcp_manager.list_all_tools()
        if tools_data:
            tool_defs.extend(ToolDef(**t) for t in tools_data)
    except Exception as exc:
        _note_failure("mcp_list_all_tools", exc)
    try:
        _mgr = _kernel()
        for name, tool in (_mgr.all_tools() if _mgr else {}).items():
            tool_defs.append(ToolDef(
                server_name="pet",
                name=name,
                description=tool.get("description", ""),
                input_schema=tool.get("parameters") or {"type": "object", "properties": {}},
            ))
    except Exception as exc:
        _note_failure("gather_plugin_tools", exc)
    # 桌面刚体（内置工具，不走插件沙箱）：推 / 拉 / 拿 / 踢。
    tool_defs.append(ToolDef(
        server_name="pet-world",
        name="pet_world_bodies",
        description="看桌面窗口刚体（不是截图；看屏幕画面用 capture_screen）：id、名字、方向和距离。视野外的看不见。",
        input_schema={"type": "object", "properties": {}},
    ))
    tool_defs.append(ToolDef(
        server_name="pet-world",
        name="pet_world_scout",
        description=(
            "看桌面窗口刚体（不是截图；看屏幕画面用 capture_screen）。"
            "从你自己的视角环顾：有哪些窗户（方向+距离+标题），可以收养一扇。"
        ),
        input_schema={"type": "object", "properties": {}},
    ))
    tool_defs.append(ToolDef(
        server_name="pet-world",
        name="pet_world_adopt",
        description=(
            "把你选中的窗户收养成刚体（之后就能走过去、站上去、推拉它）。"
            "hwnd 从 pet_world_scout 的结果里选。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "hwnd": {"type": "string", "description": "pet_world_scout 返回的窗口 hwnd"},
            },
            "required": ["hwnd"],
        },
    ))
    tool_defs.append(ToolDef(
        server_name="pet-world",
        name="pet_world_walk_to",
        description=(
            "走到你自己选中的刚体旁边（stand=true 则站到它上面）。"
            "位移由身体自动完成，你只需要选 id。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "刚体 id（从 pet_world_bodies 选）"},
                "stand": {"type": "boolean", "description": "true=站到刚体上面，默认走到旁边"},
            },
            "required": ["id"],
        },
    ))
    tool_defs.append(ToolDef(
        server_name="pet-world",
        name="pet_world_operate",
        description=(
            "操作一个桌面刚体：push 推开 / pull 拉近 / kick 踢飞 / "
            "grab 拎起来（移到猫头顶上方）。id 从 pet_world_bodies 获取。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "id": {"type": "integer", "description": "刚体 id"},
                "action": {"type": "string", "enum": ["push", "pull", "kick", "grab"]},
                "distance": {"type": "integer", "description": "可选：位移像素 40-250"},
            },
            "required": ["id", "action"],
        },
    ))
    # 自创直觉（自进化：给自己长一条新的判断方式）。
    if _intuition_registry_ref is not None:
        tool_defs.append(ToolDef(
            server_name="pet-intuition",
            name="pet_add_intuition",
            description=(
                "给自己写一条新的直觉：纯函数，输入 state，返回 0..1。"
                "state 里有 last_user_msg / mood_index / idle_sec / bodies / recent_actions。"
                "code 必须定义 `def intuition(state): ...`；不能 import、读写文件或联网。"
                "越强的信号越容易成为一条「可选闪念」（直觉苗头，可理可不理）。最多 16 条。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "1-40 字，字母/数字/_/-/中文"},
                    "description": {"type": "string", "description": "这条直觉管什么"},
                    "code": {"type": "string", "description": "def intuition(state): ..."},
                    "kind": {"type": "string", "description": "custom/emotion/space/habit"},
                    "p_emit": {"type": "number", "description": "默认浮现概率 0-0.9"},
                    "weight": {"type": "number", "description": "权重 0.1-3.0"},
                },
                "required": ["name", "description", "code"],
            },
        ))
        tool_defs.append(ToolDef(
            server_name="pet-intuition",
            name="pet_list_intuitions",
            description="列出你现在的所有直觉（内置 + 自创）及其浮现概率/权重。",
            input_schema={"type": "object", "properties": {}},
        ))
        tool_defs.append(ToolDef(
            server_name="pet-intuition",
            name="pet_remove_intuition",
            description="删除一条你自己创造的直觉（内置的三个不能删）。",
            input_schema={
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        ))
    # 自我进化工作台（M1）：先看自己，再在隔离 worktree 里提出/评测改动。
    if _evolve_service() is not None:
        tool_defs.append(ToolDef(
            server_name="pet-evolve",
            name="pet_evolve_inspect",
            description=(
                "检查你自己的代码与运行环境：模块、测试入口、插件、最近进化谱系。"
                "想改自己之前先看这个。"
            ),
            input_schema={"type": "object", "properties": {}},
        ))
        tool_defs.append(ToolDef(
            server_name="pet-evolve",
            name="pet_evolve_read",
            description=(
                "读你自己仓库里的一段源码（带行号）。path 是仓库相对路径，"
                "例如 pet-sidecar/gateway/server.py。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start": {"type": "integer", "description": "起始行，默认 1"},
                    "end": {"type": "integer", "description": "结束行（含）"},
                },
                "required": ["path"],
            },
        ))
        tool_defs.append(ToolDef(
            server_name="pet-evolve",
            name="pet_evolve_propose",
            description=(
                "提出一个真实的自我修改候选：在隔离的 git worktree 里应用 edits 并生成 diff，"
                "不会改动正在运行的代码。edits=[{path,old,new}]；old 必须精确匹配一次，"
                "新建文件时 old 为空。kind=core|plugin；核心改动在 M1 只审阅、不上线。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "为什么要改"},
                    "plan": {"type": "string", "description": "怎么改，可能的分支/风险"},
                    "title": {"type": "string"},
                    "kind": {"type": "string", "enum": ["core", "plugin"]},
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "old": {"type": "string"},
                                "new": {"type": "string"},
                            },
                            "required": ["path", "old", "new"],
                        },
                    },
                },
                "required": ["goal", "plan", "edits"],
            },
        ))
        tool_defs.append(ToolDef(
            server_name="pet-evolve",
            name="pet_evolve_evaluate",
            description=(
                "在候选 worktree 里跑聚焦测试并与基线对比，返回 "
                "verified/failed/unverified。verified 只说明没弄坏，不等于该上线。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string"},
                    "full": {"type": "boolean", "description": "跑整个 Python 测试套件（慢）"},
                },
                "required": ["candidate_id"],
            },
        ))
        tool_defs.append(ToolDef(
            server_name="pet-evolve",
            name="pet_evolve_list",
            description="列出你的进化候选与状态。",
            input_schema={
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
            },
        ))
        tool_defs.append(ToolDef(
            server_name="pet-evolve",
            name="pet_evolve_show",
            description="看一个候选的改动、评测结果和 diff（截断）。",
            input_schema={
                "type": "object",
                "properties": {"candidate_id": {"type": "string"}},
                "required": ["candidate_id"],
            },
        ))
        tool_defs.append(ToolDef(
            server_name="pet-evolve",
            name="pet_evolve_discard",
            description="丢弃一个候选并删掉它的隔离 worktree（不会碰正在运行的代码）。",
            input_schema={
                "type": "object",
                "properties": {"candidate_id": {"type": "string"}},
                "required": ["candidate_id"],
            },
        ))
    tool_defs.append(ToolDef(
        server_name="pet-body",
        name="pet_turn_head",
        description=(
            "只转头看左边或右边：身体不动，视锥方向立刻跟着转。"
            "想看清另一侧时先用它，再调用 capture_screen / pet_world_bodies。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": ["left", "right"],
                    "description": "转向哪一侧",
                },
            },
            "required": ["direction"],
        },
    ))
    tool_defs.append(ToolDef(
        server_name="pet-tempo",
        name="pet_set_tempo",
        description=(
            "决定你接下来的反应节奏（持续生效，直到过期或你再次调用）。"
            "slow=慢反应（约4–6分钟一次，默认）、fast=快反应（约1.5–2.5分钟，聊天时用）、"
            "ultra=极快反应（约50–70秒，活跃对话时用）。默认慢反应。"
        ),
        input_schema={
            "type": "object",
            "properties": {
                "mode": {
                    "type": "string",
                    "enum": list(_TEMPO_MODES),
                    "description": "接下来的反应节奏",
                },
            },
            "required": ["mode"],
        },
    ))
    if core_only:
        tool_defs = [t for t in tool_defs if t.name in CORE_TOOL_NAMES]
    return tool_defs or None


def _real_user_turn_text(req: ChatRequest) -> Optional[str]:
    """Text of the last message when this is a real user turn, else None.

    Narration (``silent``), empty requests and renderer-synthesised
    proactive wake-ups (``[系统提示…]``) must not reach the Laya budget
    router: they are not something the user said, so their budget keeps the
    legacy provider behaviour.
    """
    if getattr(req, "silent", False):
        return None
    messages = getattr(req, "messages", None) or []
    if not messages:
        return None
    last = messages[-1]
    role = last.get("role") if isinstance(last, dict) else getattr(last, "role", None)
    if role != "user":
        return None
    content = last.get("content") if isinstance(last, dict) else getattr(last, "content", None)
    if isinstance(content, list):
        content = next(
            (p.get("text") for p in content if isinstance(p, dict) and p.get("type") == "text"),
            "",
        )
    text = str(content or "")
    if text.lstrip().startswith(_PROACTIVE_PROMPT_PREFIX):
        return None
    return text


def _should_log_conversation(req: ChatRequest, conversation_dir: Optional[Path]) -> bool:
    """True only for a real user turn.

    Narration (``silent``), debug chat and empty requests share the same
    streaming path. They must never enter ``conversations/*.jsonl`` —
    sleep compression treats that file as *the user's* day, and a narrator
    prompt logged as "user" would poison the memory it is supposed to feed.
    """
    if conversation_dir is None:
        return False
    return _real_user_turn_text(req) is not None


def _extract_memory_section(
    episodic_ctx: str, marker: str, *, limit: int
) -> str:
    """Return one section of the gateway's episodic memory context."""
    text = str(episodic_ctx or "")
    start = text.find(marker)
    if start < 0:
        return ""
    tail = text[start + len(marker):]
    for stop in (
        "\n【突然浮现在脑海中的事】",
        "\n【你经历过但记不清细节的事】",
        "\n【你记得的事】",
        "\n【你一直放在心上的事】",
        "\n（现在是",
    ):
        cut = tail.find(stop)
        if cut >= 0:
            tail = tail[:cut]
    return (marker + tail).strip()[:limit]


def _extract_flashback_pulse(episodic_ctx: str) -> str:
    """Keep the probabilistic flashback section for a web delta."""
    return _extract_memory_section(
        episodic_ctx, "【突然浮现在脑海中的事】", limit=800
    )


def _extract_core_pulse(episodic_ctx: str) -> str:
    """A compact identity anchor repeated in every web delta."""
    return _extract_memory_section(
        episodic_ctx, "【你一直放在心上的事】", limit=500
    )


def _recent_dialogue_pulse(messages: list[dict], limit: int = 4) -> str:
    """Keep recent real dialogue, not repeated autonomous wake-up prompts."""
    lines: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text") or "") for part in content
                if isinstance(part, dict) and part.get("type") == "text"
            )
        text = str(content or "").strip()
        role = str(message.get("role") or "user")
        if not text:
            continue
        if role == "user" and (
            text.lstrip().startswith("[系统提示")
            or re.match(r"^\[[^\]]+ 结果\]", text)
        ):
            continue
        lines.append(("用户" if role == "user" else "你") + "：" + text[:200])
    return "\n".join(lines[-limit:])


def _budgeted_web_pulse(parts: list[tuple[str, int]], limit: int = 2400) -> str:
    """Reserve room for required current state before associative memories."""
    kept: list[str] = []
    used = 0
    for text, budget in parts:
        text = str(text or "").strip()
        if not text:
            continue
        if len(text) > budget:
            text = text[:budget].rstrip() + "\n…"
        remaining = limit - used
        if remaining <= 0:
            break
        if len(text) > remaining:
            text = text[:remaining].rstrip()
        kept.append(text)
        used += len(text)
    return "\n\n".join(kept).strip()


def _execution_receipt(name: str, result, *, body: bool = False) -> dict:
    """Small executor-only evidence; never retain arguments, images or OCR text."""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (TypeError, ValueError):
            result = {"summary": result}
    data = result if isinstance(result, dict) else {}
    raw_status = str(data.get("status") or "").lower()
    error = bool(data.get("is_error") or data.get("isError")) or raw_status in {
        "error", "failed", "failure", "denied", "blocked", "cancelled", "canceled",
        "unsupported", "unavailable", "timeout",
    }
    applied = data.get("applied") is True and raw_status == "ok" and not error
    status = "error" if error else (
        "not_applied" if not data or raw_status == "not_applied"
        or data.get("applied") is False or (body and not applied) else "ok"
    )
    # No payload excerpts: even a 'summary' may contain private screen text.
    if body:
        summary = "身体已确认执行" if applied else "身体动作未完成"
        if applied:
            facing = data.get("facing")
            if facing in ("left", "right"):
                summary += "；朝向=" + facing
            coords = [data.get(axis) for axis in ("x", "y")]
            if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in coords):
                summary += f"；位置=({coords[0]}, {coords[1]})"
    elif name in ("capture_screen", "observe_screen"):
        hint = str(data.get("summary") or "").lower()
        if not error and name == "capture_screen" and "no vision channel" in hint:
            status = "not_applied"
        if status != "ok":
            summary = "屏幕工具未完成；没有获得可用图像/解析结果"
        elif name == "observe_screen":
            summary = "屏幕解析工具返回结果；不是直接看见图像"
        elif data.get("screenshot_base64"):
            summary = "截图工具返回图像；不代表已经理解画面"
        else:
            summary = "视觉通道返回描述；不是当前模型直接看见图像"
    elif name in ("pet_world_scout", "pet_world_bodies"):
        summary = "窗口几何查询失败；没有获得图像" if error else "仅获取窗口几何/刚体列表，不是看见屏幕图像"
    else:
        summary = "工具执行失败" if error else "工具返回结果"
    receipt = {"name": name, "kind": "body" if body else "tool", "status": status, "summary": summary}
    if body:
        receipt["applied"] = applied
    return receipt


async def _stream_chat_provider(
    registry: ProviderRegistry,
    mcp_manager: MCPManager,
    bridge: ClawdBridge,
    req: ChatRequest,
    server: LlamaServer,
    server_state: dict,
    mood_store: Optional[MoodStore] = None,
    lora_arr: Optional[list[dict]] = None,
    conversation_dir: Optional[Path] = None,
) -> AsyncGenerator[bytes, None]:
    """Provider-based streaming chat with MCP tool support."""
    global _last_real_user_ts, _last_user_msg, _last_user_ts, _proactive_streak
    log = get_logger()
    turn_started = time.perf_counter()
    _round_started_at = time.time()
    first_token_recorded = False
    telemetry.inc("chat.turns")

    # ── Brain stream (diagnostics / brain window) ─────────────────────
    # Side channel only: every publish is failure-isolated so a bad event,
    # a full subscriber queue or a broken UI stream can never break chat.
    _round_id = "r_" + secrets.token_hex(4)
    iteration = 0
    _tool_call_count = 0
    _inner_chars = 0
    _d_summary = ""
    _d_decision_text = ""
    _round_receipts: list[dict] = []
    _round_inner_parts: list[str] = []
    _control_failure_pending = False
    _control_feedback_sent = False
    _iteration_surface: list[str] = []

    def _record_receipt(name: str, result, *, body: bool = False) -> dict:
        receipt = _execution_receipt(name, result, body=body)
        _round_receipts.append(receipt)
        return receipt

    def _emit(kind: str, title: str, text: str = "", **meta) -> None:
        try:
            _brain.publish({
                "round": _round_id,
                "iteration": iteration,
                "type": kind,
                "title": title,
                "text": text,
                "meta": meta,
            })
        except Exception:
            pass

    # One definition of "this is not a real user turn" for the whole round.
    # Proactive wake-ups, silent narration and empty requests are quiet: they
    # may legitimately end with no visible text.
    _user_turn_text = _real_user_turn_text(req)
    _is_proactive = _user_turn_text is None
    # ── 生命体征短路（强制停机 / 睡眠抑制 / 已死亡）──────────────────────
    # Only the PROACTIVE path is blocked (sleeping / dead / discharged bodies
    # don't tick on their own); real user turns always go through — point 5
    # handles the wake ladder and death there. No provider call, no ledger
    # tick, no drain: no work = no round.
    _vitals0 = get_vitals()
    if (
        _vitals0 is not None
        and not req.silent
        and _user_turn_text is None
        and (bool(_vitals0.forced_off) or bool(_vitals0.died) or bool(_vitals0.is_asleep()))
    ):
        if bool(_vitals0.died):
            _sleep_state = "died"
        elif bool(_vitals0.forced_off):
            _sleep_state = "forced_off"
        else:
            _sleep_state = "asleep"
        log.info(
            "vitals short-circuit proactive round: died=%s forced_off=%s asleep=%s",
            bool(_vitals0.died), bool(_vitals0.forced_off), bool(_vitals0.is_asleep()),
        )
        _emit("round_start", "轮开始", proactive=True, silent=False, messages=len(req.messages or []), model_provider=req.model_provider or "auto", user_text="", skipped="vitals")
        _emit("round_end", "本轮结束", elapsed_ms=(time.perf_counter() - turn_started) * 1000.0, iterations=0, tools=0, provider=req.model_provider or "auto", generated=False, skipped="vitals")
        # 机器事件替代可见气泡：睡着/停机/死亡期间每个主动拍不再弹出
        # "（睡着了……）"文本（slow 节奏下 8 小时睡眠 ≈ 96 个气泡）。
        # 渲染器消费 sleep_state {state} 显示一次性 💤/🔋 指示：状态不变
        # 不重复，正常完成的轮次（不带该事件）让它自愈。未知事件在旧渲染
        # 器上被安全忽略——不比旧行为差。
        yield _sse({"event": "start"})
        yield _sse({"event": "sleep_state", "state": _sleep_state})
        yield _sse({"event": "end"})
        return
    # Capture receipt time before D/provider/tool awaits, not after generation.
    # Life prompts and silent narration must never become the latest human.
    if not req.silent and _user_turn_text:
        _last_user_msg = _user_turn_text
        _last_real_user_ts = _round_started_at
        _last_user_ts = _round_started_at
    _continuity_store = None if req.silent else _get_dstate_store()
    # Set at the D-core fork below: this round was short-circuited BEFORE the
    # provider by the vitals gate (asleep / forced-off / died on a proactive
    # tick). A short-circuited tick is not a life round: no drain, no ledger
    # tick, no ledger entry — the finally block reads this flag.
    _vitals_short_circuited = False
    # ── 上一轮 tick 留下的待办（点 7/9）──────────────────────────────
    # organize_kind：上一轮 round_tick 刚跨过 500 的倍数 → 本轮（下一个
    # 主动轮）变成记忆整理轮，读到即消费（本轮 finally 的 tick 会重写
    # 这个槽位）。overflow：slow 溢出闩锁仍在 → 本轮升级休息提醒。
    _pending_organize_kind: Optional[str] = (
        str(_pending_vitals_round.get("organize_kind") or "") or None
    )
    _pending_overflow = bool(_pending_vitals_round.get("overflow"))
    # Only a PROACTIVE round consumes the organize slot: a real user turn
    # must be answered as asked — it neither runs the organize prompt nor
    # clears the pending mark (the next proactive round picks it up; the
    # finally's tick below never overwrites a still-pending slot).
    if _pending_organize_kind and _user_turn_text is None:
        _pending_vitals_round["organize_kind"] = None  # consumed by THIS round
    if _continuity_store is not None and _user_turn_text:
        try:
            _continuity_store.note_user(_user_turn_text, _round_id, _round_started_at)
            _continuity_store.save()
        except Exception as exc:
            _note_failure("continuity_note_user", exc)
    # Thinking stays private; idle completes without resolving a provider.
    _quiet_life_turn = False
    _d_tempo_mode = ""
    _tempo_source = "derive"
    # Per-round temperature base from the resolved tempo spec. None ⇒ the
    # request's own temperature stands (see the chat_kwargs composition).
    _tempo_base_temperature: Optional[float] = None
    # Chat-mode entry (聊天节奏): a real user turn switches the AI to a
    # faster tempo — "ultra" when another user turn arrived within the
    # conversation gap (active dialogue), else "fast". Written into the
    # tempo store with the normal TTL, so the NEXT rounds (including
    # proactive ones) keep the cadence until it expires. Runs for every
    # round shape, with or without a D store.
    if _user_turn_text is not None:
        _d_tempo_mode = _note_user_turn_for_tempo(time.time())
        _tempo_source = "chat"
        _DENSITY_STATE["last_user_turn_at"] = time.time()
    elif not req.silent:
        # Silent narration (narrator) is not a life round: it keeps the old
        # no-tempo-event shape and its own temperature path.
        _d_tempo_mode, _tempo_source = _resolve_tempo(time.time())
    # An explicitly requested temperature (anything ≠ the ChatRequest
    # default) outranks the tempo base: the caller asked for a specific
    # sampling temperature, so no tempo/emotion reinterpretation happens.
    try:
        _explicit_temperature = (
            float(req.temperature)
            != float(ChatRequest.model_fields["temperature"].default)
        )
    except (TypeError, ValueError):
        _explicit_temperature = False
    # The resolved mode's temperature base applies to this round — including
    # a chat-entry round (fast/ultra), unless the caller pinned a temperature.
    if not _explicit_temperature:
        try:
            _tempo_base_temperature = float(_TEMPO_SPECS[_d_tempo_mode]["temp"])
        except (KeyError, TypeError, ValueError):
            _tempo_base_temperature = None

    _round_user_text = ""
    try:
        if req.messages:
            _last_round_content = req.messages[-1].content
            if isinstance(_last_round_content, list):
                _last_round_content = next(
                    (
                        part.get("text")
                        for part in _last_round_content
                        if isinstance(part, dict) and part.get("type") == "text"
                    ),
                    "",
                )
            _round_user_text = str(_last_round_content or "")[:500]
    except Exception:
        _round_user_text = ""
    _emit(
        "round_start",
        "轮开始",
        proactive=_is_proactive,
        silent=bool(req.silent),
        messages=len(req.messages or []),
        model_provider=req.model_provider or "auto",
        user_text=_round_user_text,
    )

    # A real user turn outranks the previous turn's background extraction.
    # On a single-slot local engine the fallback would otherwise prefill
    # ahead of the answer the user is waiting for.
    if _real_user_turn_text(req) is not None:
        _cancel_pending_fallbacks()

    # ── D 核心（快直觉）：先问 D，再决定 provider 的思考/篇幅/动作 ──
    # MUST run before _resolve_provider: LocalProvider consumes
    # enable_thinking at construction time. ``_cortex_store`` doubles as the
    # "D is active this turn" flag (it also gates the meta seat below and
    # the DSTATE parse at the end of the round).
    _cortex_store = None
    _d_budget = None
    if not req.silent and _cortex_enabled():
        _cortex_store = _continuity_store
    # ── 快直觉 reflex 门（System 0）是否武装 ─────────────────────────
    # 提前到 D 核心之前算好：D 的 Laya 回落告警要据此静音——reflex 在
    # 场时它才是 D 的唯一决策者，"生命轮只能保持安静"的告警就是假的。
    _rx_cfg = get_reflex_config()
    _rx_gate_armed = (
        _cortex_store is not None
        and _is_proactive
        and not _pending_organize_kind
        and _rx_cfg is not None
        and bool(_rx_cfg.enabled)
    )
    # ── 睡梦中的用户说话（吵醒阶梯；vitals 点 5）─────────────────────
    # THE only user-wake entry. Each call = one 吵醒 = 1.0 HP damage and a
    # sleep-plan multiplier inside the module; the reply goes out normally
    # (allow_reply is always True), the episode simply continues with its
    # extended plan. ``think`` (stage ≥ 2) overrides whatever D decides for
    # this round; the stage-1 somatic line rides into the system prompt with
    # the other context injections below.
    _wake: Optional[dict] = None
    if _vitals0 is not None and not req.silent:
        try:
            _wake = _vitals0.user_speech_wake()
        except Exception as exc:
            _note_failure("vitals_user_speech_wake", exc)
            _wake = None
        if _wake is not None:
            req.thinking = bool(_wake.get("think"))
            log.info(
                "vitals: user speech wake (stage=%s think=%s hp=%.1f)",
                _wake.get("stage"), bool(_wake.get("think")), _vitals0.hp,
            )
            # 被吵醒≠醒来：AI 回应后还会继续睡（episode 延长），壳应保持
            # sleeping 动画而不是切回 idle。只有自然醒才回 idle。
            try:
                bridge.post("sleeping")
            except Exception:
                pass
            try:
                if _vitals0.died and _should_archive_death(_vitals0):
                    _grave_dir = _vitals_state_dir()
                    grave = _vitals0.graveyard_archive(
                        _grave_dir
                        if _grave_dir is not None
                        else _default_memory_root()
                    )
                    # 入葬后换新台账：graveyard_archive 只重置磁盘上的
                    # ledger.json（条目清空、seq/round_count 保留），内存
                    # 单例仍握着旧灵魂的条目——下一次落账会把它们复活到
                    # 磁盘。重建单例让它加载刚重置的文件，并清掉待办
                    # organize 槽位（旧身体的记忆审查不属于新身体）。
                    try:
                        set_ledger(
                            _Ledger(
                                _grave_dir
                                if _grave_dir is not None
                                else _default_memory_root()
                            )
                        )
                        _pending_vitals_round["organize_kind"] = None
                        _pending_vitals_round["overflow"] = False
                    except Exception as exc:
                        _note_failure("ledger_fresh_after_grave", exc)
                    log.warning(
                        "vitals: 宠物死亡入葬 %s（记忆已归档，身体重置）", grave
                    )
                    _wake = None  # fresh body: this round runs as a normal user turn
            except Exception as exc:
                _note_failure("vitals_graveyard_archive", exc)
    if _cortex_store is not None:
        _cortex_state = {
            "user_text": _user_turn_text or "",
            "is_proactive": _is_proactive,
            "mood_index": mood_store.emotion_index if mood_store is not None else 0,
            "idle_sec": (
                max(0, time.time() - _last_real_user_ts)
                if _last_real_user_ts
                else 0
            ),
            "now": time.time(),
            "last_user": dict(_cortex_store.last_user or {}),
            "last_round": dict(_cortex_store.last_round or {}),
            "provider_type": "web" if (req.model_provider or "").lower() == "deepseek-web" else "fast",
        }
        _cortex_started = time.perf_counter()
        try:
            _decision = await _cortex.passive_decision(
                _cortex_store,
                _cortex_state,
                timeout=_cortex_round_timeout(_cortex_store, _is_proactive),
            )
            if not isinstance(_decision, dict):
                raise TypeError("cortex decision is not a dict")
        except Exception as exc:
            telemetry.inc("decision.cortex.errors")
            log.warning("cortex decision failed (safe fallback): %s", exc)
            _decision = {
                "thinking": False,
                "reasoning_effort": None,
                "strength": "no_thinking",
                "reaction": "medium",
                "max_tokens": _cortex.REACTION_MAX_TOKENS["medium"],
                "action": "quiet" if _is_proactive else "speak",
                "action_label": "安静" if _is_proactive else "说",
                "source": "fallback",
                "confidence": 0.0,
                "reason": f"fallback:{type(exc).__name__}",
            }
        _cortex_latency_ms = (time.perf_counter() - _cortex_started) * 1000.0

        # A real user turn is a hard response obligation: never let the
        # default action "quiet" swallow the answer.
        if not _is_proactive and _decision.get("action") in {"quiet", "think"}:
            _decision["action"] = "speak"
            _decision["action_label"] = "说"
            _decision["reason"] = (
                str(_decision.get("reason") or "") + "|forced_speak:user_turn"
            )

        # A thinking life round generates inner voice without visible speech.
        _quiet_life_turn = bool(
            _is_proactive and (_decision.get("action") == "think")
        )

        _decision["thinking"] = bool(_decision.get("thinking"))
        _decision["reasoning_effort"] = (
            "low" if _decision["thinking"] else None
        )
        if _decision.get("reaction") not in _cortex.REACTION_MAX_TOKENS:
            _decision["reaction"] = "medium"
        try:
            _max_tokens = int(_decision.get("max_tokens") or 0)
        except (TypeError, ValueError):
            _max_tokens = 0
        if _max_tokens <= 0:
            _max_tokens = _cortex.REACTION_MAX_TOKENS[_decision["reaction"]]
        _decision["max_tokens"] = _max_tokens

        # Reflex layer: on autonomous rounds, a reflex-weight S answer that is
        # a clear body direction (left/right) moves the body directly, without
        # waiting for the main model — a glance, not a decision. Never on a
        # real user turn (that would be sleepwalking over the conversation).
        if _is_proactive and _decision.get("action") != "quiet":
            for _s in _decision.get("s_answers") or []:
                if _s.get("weight") != "reflex":
                    continue
                _choice = str(_s.get("choice") or "").strip().lower()
                _dir = _cortex.REFLEX_DIRECTIONS.get(_choice)
                if _dir:
                    try:
                        _r = await _pet_action("turn", direction=_dir)
                    except Exception:
                        _r = {"status": "error", "applied": False}
                    _record_receipt("reflex_turn", _r, body=True)
                    if _r.get("status") == "ok" and _r.get("applied") is True:
                        _s["applied"] = _dir
                        _WORLD_VIEW_CACHE["t"] = 0.0
                        _remember_body_action("turn", _r)

        req.thinking = _decision["thinking"]
        req.reasoning_effort = (
            _decision["reasoning_effort"] if req.thinking else None
        )
        req.max_new_tokens = _max_tokens
        # Stage-1 吵醒强制不思考（点 5）：用户说话唤醒的优先级高于 D——
        # 半梦半醒的身体没有余力进入深思考；stage ≥ 2 的 wake["think"]=True
        # 已在 req.thinking 里，D 若也判 thinking 则保持。
        if _wake is not None and not bool(_wake.get("think")):
            req.thinking = False
            req.reasoning_effort = None
        try:
            _d_confidence = float(_decision.get("confidence") or 0.0)
        except (TypeError, ValueError):
            _d_confidence = 0.0
        # ── 节奏（tempo）本轮决议 ─────────────────────────────────────
        # The store entry (model pet_set_tempo / chat entry, resolved above)
        # already won or the resting cadence was derived — this is just the
        # per-round audit log. Idle/reaction/action no longer steer tempo
        # (fatigue/rest is a separate battery system's job).
        _d_action = str(_decision.get("action") or "speak")
        _d_reaction = str(_decision.get("reaction") or "medium")
        try:
            _d_idle = float((_cortex_state or {}).get("idle_sec") or 0.0)
        except (TypeError, ValueError):
            _d_idle = 0.0
        log.info(
            "cortex.tempo mode=%s source=%s idle=%.0fs reaction=%s action=%s",
            _d_tempo_mode, _tempo_source, _d_idle, _d_reaction, _d_action,
        )
        _d_budget = BudgetDecision(
            tier="cortex",
            thinking=req.thinking,
            reasoning_effort=req.reasoning_effort or "low",
            max_tokens=req.max_new_tokens,
            source="cortex",
            confidence=_d_confidence,
            reason=str(_decision.get("reason") or ""),
        )
        log.info(
            "cortex.decision source=%s strength=%s reaction=%s action=%s "
            "max_tokens=%d latency_ms=%.1f reason=%s",
            _decision.get("source"),
            _decision.get("strength"),
            _decision.get("reaction"),
            _decision.get("action"),
            _max_tokens,
            _cortex_latency_ms,
            _decision.get("reason") or "-",
        )
        if _is_proactive:
            if _decision.get("source") == "laya+prior":
                _D_INTUITION_ALERT["streak"] = 0
            elif _decision.get("source") == "prior":
                _D_INTUITION_ALERT["streak"] += 1
                # reflex 武装时 D 的先验只是参考，安静轮由小模型裁决——
                # 这时报"生命轮只能保持安静"既假又刷屏，静默计数即可。
                if _D_INTUITION_ALERT["streak"] == 3 and not _rx_gate_armed:
                    _reason = str(_decision.get("reason") or "-")
                    log.warning(
                        "D intuition unavailable for 3 life rounds: %s", _reason
                    )
                    _emit(
                        "error",
                        "直觉不可用",
                        "Laya 直觉引擎连续 3 轮没有响应，生命轮只能保持安静。"
                        "检查 Laya sidecar 是否在运行。",
                        reason=_reason,
                    )
                    telemetry.inc("decision.cortex.laya_fallback")
        try:
            telemetry.inc("decision.cortex.total")
            telemetry.inc(
                "decision.cortex.source." + str(_decision.get("source") or "unknown")
            )
            telemetry.gauge("decision.cortex.latency_ms", _cortex_latency_ms)
            telemetry.gauge("decision.cortex.max_tokens", _max_tokens)
            telemetry.gauge(
                "decision.cortex.strength",
                1.0 if _decision.get("strength") == "low" else 0.0,
            )
            telemetry.gauge(
                "decision.cortex.reaction",
                {"small": 0.0, "medium": 1.0, "large": 2.0}.get(
                    str(_decision.get("reaction")), 1.0
                ),
            )
            telemetry.gauge(
                "decision.cortex.action",
                {
                    "speak": 0.0,
                    "walk": 1.0,
                    "look": 2.0,
                    "quiet": 3.0,
                    "other": 4.0,
                }.get(str(_decision.get("action")), 4.0),
            )
        except Exception:
            pass

    # ── D 决策最终发布（必须在 reflex 覆盖之后）───────────────────────
    # 面板和主脑 prompt 必须看到真正裁决这一轮的那份决策：reflex 在场
    # 时它就是唯一决策者（D 的先验采样只是参考）。所以 summary/d_decision
    # 事件与 req.system 注入都挪到 reflex 门之后；reflex-quiet 路径在
    # return 前自己调用一次。
    # 面板的"延迟"必须报真正裁决者的耗时：reflex 在场时报小模型的整拍
    # 耗时，而不是 D 那条（已经没用的）Laya 尝试的 2 秒。
    _d_latency_ms: Optional[float] = None

    def _publish_d_decision() -> str:
        nonlocal _d_summary, _d_decision_text
        if _cortex_store is None:
            return ""
        try:
            _d_summary = str(_cortex_store.summary or "（还没有）")
            _d_decision_text = _cortex.decision_to_prompt(_decision)
            _emit(
                "summary",
                "D 读到的生命总结",
                _d_summary,
                chars=len(_d_summary),
            )
            _emit(
                "d_decision",
                "D 决策",
                _d_decision_text,
                strength=_decision.get("strength"),
                reaction=_decision.get("reaction"),
                action=_decision.get("action"),
                action_label=_decision.get("action_label"),
                max_tokens=_decision.get("max_tokens"),
                source=_decision.get("source"),
                confidence=_decision.get("confidence"),
                latency_ms=(
                    _d_latency_ms if _d_latency_ms is not None
                    else _cortex_latency_ms
                ),
                summary_chars=len(_d_summary),
                decision_id=_decision.get("decision_id"),
            )
            return (
                "\n\n【你的底层直觉状态（只用于行动，不要在回复里复述）】\n"
                "上一轮生命总结（主观想法，可能错；不是摄像头或执行事实；用它更新本轮 DSTATE）：\n"
                f"{_d_summary}\n"
                f"{_d_decision_text}\n"
                "用户直接说话时必须回应；默认动作只是默认，你可以改。"
            )
        except Exception as exc:
            _note_failure("cortex_prompt_injection", exc)
            return ""

    # ── 快直觉 reflex 门（System 0）：主动轮的升级判定 ────────────────
    # The gate covers EVERY proactive round (economics: D's non-quiet
    # actions historically fire on ~46% of ticks — letting them wake the
    # main brain directly empties the battery in half a day; the main
    # round must become the scarce event). D still ran above (its
    # bookkeeping incl. record_round ordering is preserved) and stays
    # advisory; the tiny local LLM decides whether this tick is spent by
    # the body alone (reflex-only quiet round) or wakes the main brain
    # (fall through into the full pipeline). Skip conditions: real user
    # turns, silent narration, rounds short-circuited by the vitals gate
    # above (asleep / forced-off / died) and pending organize rounds —
    # an organize round is main-brain by definition, the gate must not
    # eat it. judge() NEVER raises and every failure comes back as
    # source="fallback" quiet, so the gate cannot break a life tick.
    # Reflex layer absent or disabled → nothing here runs: a D-quiet round
    # falls through to the old early return right after this block, a
    # D-non-quiet round proceeds to the main brain as today.
    # (`_rx_cfg` / `_rx_gate_armed` are computed before the D core so the
    # Laya-fallback alert can be silenced while the reflex answers.)
    _reflex_decision: Optional[dict] = None
    if _rx_gate_armed:
        # Reflex state snapshot — only what is already in hand this round
        # (D summary / idle / mood index / ledger fast entries). new_events
        # stays 0: proactive rounds by definition have no fresh user
        # message. The D advisory (d_action) is not a known prompt key
        # (reflex.build_user_prompt serializes fixed lines only), so it is
        # appended to the summary tail instead of passed as a custom key.
        try:
            _rx_mood_idx = float(
                (mood_store.emotion_index if mood_store is not None else 0.0) or 0.0
            )
        except (TypeError, ValueError):
            _rx_mood_idx = 0.0
        _rx_summary = str(_cortex_store.summary or "").strip()
        _rx_advice = "；D 建议：" + str(_decision.get("action") or "quiet")
        _rx_summary = (_rx_summary + _rx_advice) if _rx_summary else ("（暂无摘要）" + _rx_advice)
        # 最近心声：取台账的 fast 条目（快直觉轮的一行记录）。安静轮不再
        # 产瞬时记忆（D-quiet 在 provider 前短路；reflex-quiet 落的是台账
        # fast），瞬时记忆在长安静期只会是空的/过期的——台账才是这颗心智
        # 最近想过的东西。取最近 ≤3 条，按时间顺序。
        _rx_thoughts: list = []
        try:
            _rx_lg_pre = get_ledger()
            if _rx_lg_pre is not None:
                _rx_thoughts = [
                    t for t in (
                        str(e.get("text") or "").strip()
                        for e in _rx_lg_pre.entries()
                        if e.get("kind") == "fast"
                    ) if t
                ][-3:]
        except Exception:
            _rx_thoughts = []
        # 真实上下文（不再硬编码 False/0）：用户最近是否在场、本轮之前有
        # 多少新事件——否则用户刚说完"你叫我别睡"，下一拍小模型看到的
        # 还是"用户不在、零新事件"→ 校准指导告诉它 quiet → 刚聊完就装不认识。
        _now_ts = time.time()
        _last_user_at = float(_DENSITY_STATE.get("last_user_turn_at") or 0.0)
        _user_ago = max(0.0, _now_ts - _last_user_at) if _last_user_at > 0 else 9999.0
        _user_present = _user_ago <= 120.0  # 2 分钟内说过话 = 在场
        _last_round_at = float(_DENSITY_STATE.get("last_round_at") or 0.0)
        _new_events = sum(
            1 for ts, _ in _DENSITY_EVENTS
            if (_last_round_at <= 0.0 or ts > _last_round_at)
            and 0.0 <= _now_ts - ts <= _DENSITY_EVENT_WINDOW_S
        )
        _reflex_state = {
            "summary": _rx_summary,
            "tempo_mode": _d_tempo_mode or "slow",
            "new_events": _new_events,
            "user_present": _user_present,
            "idle_s": _d_idle,
            "mood": _mood_label_for_reflex(_rx_mood_idx),
            "recent_thoughts": _rx_thoughts,
            "d_action": str(_decision.get("action") or "quiet"),
            "user_ago_s": round(_user_ago, 1) if _last_user_at > 0 else None,
        }
        # 授时闭环：只有上一轮小模型自己标了 time_relevant（它说"我需要
        # 知道时间"），这一轮才附上当前时间和距上次主脑轮的时长。不主动
        # 报时——时间感是模型自己的选择，不是系统塞给它的。闩锁必须跨轮
        # 持久化：本轮 `_reflex_decision` 还没生成（下面才 judge），所以
        # 这里读的是 _REFLEX_TIME_STATE（上一拍写进去的）。
        if bool(_REFLEX_TIME_STATE.get("wanted")):
            _reflex_state["current_time"] = datetime.now().strftime("%H:%M")
            _last_main_at = float(_DENSITY_STATE.get("last_round_at") or 0.0)
            if _last_main_at > 0:
                _reflex_state["elapsed_since_main_s"] = max(
                    0.0, time.time() - _last_main_at
                )
        _rx_started = time.perf_counter()
        _reflex_decision = await _reflex_module.judge(
            _reflex_state, _rx_cfg
        )
        _reflex_latency_s = (time.perf_counter() - _rx_started)
        _d_latency_ms = _reflex_latency_s * 1000.0
        log.info(
            "reflex tick action=%s think=%s next=%ss lat=%.1fs src=%s fail=%s",
            _reflex_decision.get("action"),
            bool(_reflex_decision.get("think")),
            _reflex_decision.get("next_check_s"),
            _reflex_latency_s,
            _reflex_decision.get("source"),
            _reflex_decision.get("failure") or "-",
        )
        try:
            telemetry.inc("reflex.ticks")
            if _reflex_decision.get("source") == "fallback":
                telemetry.inc("reflex.fallback")
        except Exception:
            pass
        # 小模型的时间意志落盘：它这一拍说"我需要时间"，下一拍系统才授时。
        # 不能放局部变量——下一拍 `_reflex_decision` 会被重置成 None。
        _REFLEX_TIME_STATE["wanted"] = bool(_reflex_decision.get("time_relevant"))
        _REFLEX_TIME_STATE["asked_at"] = (
            time.time() if _REFLEX_TIME_STATE["wanted"] else 0.0
        )
        # ── reflex 决策覆盖 D 先验采样（安静轮 + 升级轮都要）─────────────
        # 用户裁定：D 的思考强度/持续反应/默认动作由直觉小模型决定，D 的
        # 先验采样只作参考。覆盖必须发生在发布/注入/req 赋值之前——旧代码
        # 把覆盖放在升级分支末尾，req 在更早的 6389 行就已按 D 先验写好，
        # 所以小模型的 think/reaction 从来没到过 provider。
        _decision["thinking"] = bool(_reflex_decision.get("think"))
        _decision["reasoning_effort"] = (
            "low" if _reflex_decision.get("think") else None
        )
        _decision["strength"] = (
            "low" if _reflex_decision.get("think") else "no_thinking"
        )
        _decision["reaction"] = str(_reflex_decision.get("reaction") or "small")
        _decision["max_tokens"] = _cortex.REACTION_MAX_TOKENS.get(
            _decision["reaction"], 2048
        )
        _rx_action = str(_reflex_decision.get("action") or "quiet")
        _rx_action_label = _rx_action
        try:
            for _o in _cortex_store.action_options or []:
                if str(_o.get("id")) == _rx_action:
                    _rx_action_label = str(_o.get("label") or _rx_action)
                    break
        except Exception:
            pass
        _decision["action"] = _rx_action
        _decision["action_label"] = _rx_action_label
        # 来源要区分"小模型真的判了"和"小模型没答、走了安全回落"：前者是
        # reflex，后者标 reflex·回落——否则面板显示"来源 reflex"而实际
        # 每拍都在超时回落，看起来像小模型在决策，其实根本没答上。
        _decision["source"] = (
            "reflex" if _reflex_decision.get("source") == "reflex"
            else "reflex·回落"
        )
        # reflex 给的是离散判断，不是采样后验：不展示 D 的先验置信度。
        _decision["confidence"] = None
        _decision["reason"] = f"reflex|{_reflex_decision.get('reason') or ''}"
        # req 与 BudgetDecision 都是按 D 先验赋的值，必须同步重写，否则
        # provider 侧（_output_budget 以 _d_budget 为准）仍拿旧预算。
        req.thinking = bool(_decision["thinking"])
        req.reasoning_effort = (
            _decision["reasoning_effort"] if req.thinking else None
        )
        req.max_new_tokens = int(_decision["max_tokens"])
        # Stage-1 吵醒强制不思考优先于小模型：半梦半醒的身体没有余力深想。
        if _wake is not None and not bool(_wake.get("think")):
            req.thinking = False
            req.reasoning_effort = None
        _quiet_life_turn = bool(
            _is_proactive and (_decision.get("action") == "think")
        )
        if _d_budget is not None:
            _d_budget = BudgetDecision(
                tier="reflex",
                thinking=req.thinking,
                reasoning_effort=req.reasoning_effort or "low",
                max_tokens=req.max_new_tokens,
                source="reflex",
                confidence=0.0,
                reason=str(_decision.get("reason") or ""),
            )
        log.info(
            "reflex decision override: thinking=%s reaction=%s action=%s "
            "max_tokens=%d reason=%s time_wanted=%s",
            _decision["thinking"],
            _decision["reaction"],
            _decision["action"],
            _decision["max_tokens"],
            _decision["reason"],
            bool(_REFLEX_TIME_STATE.get("wanted")),
        )
        # Reflex-only round: the body keeps the tick AND stays silent —
        # quiet action (or the unbreakable fallback quiet) with think=false.
        # No provider, no main drain; the reflex layer bills its own small
        # constant (prompt side ≈ system + state lines ≈ 300 tokens,
        # completion side = config.max_tokens) and records ONE fast entry.
        # The finally block does nothing for this round (_vitals_short_circuited).
        if (
            _reflex_decision.get("action") == "quiet"
            and not bool(_reflex_decision.get("think"))
        ):
            _cortex_store.record_round(_round_id, _round_started_at, time.time(), True, "", "", _round_receipts)
            _cortex_store.save()
            # 发布最终决策（已被 reflex 覆盖）：面板必须显示"来源 reflex"，
            # 而不是上一拍 D 先验采样留下的 prior/0%。
            _publish_d_decision()
            _emit(
                "quiet",
                "静 · 快直觉安静轮",
                hidden_chars=0,
                reason="reflex",
                generated=False,
                reflex_action=_reflex_decision.get("action"),
                reflex_source=_reflex_decision.get("source"),
            )
            _emit(
                "round_end",
                "本轮结束",
                elapsed_ms=(time.perf_counter() - turn_started) * 1000.0,
                iterations=0,
                tools=0,
                provider=req.model_provider or "auto",
                generated=False,
            )
            telemetry.observe("chat.turn_seconds", time.perf_counter() - turn_started)
            _vitals_short_circuited = True  # reflex-quiet: no provider, no life-round tick
            yield _sse({"event": "start"})
            yield _sse({"event": "tempo", "mode": _d_tempo_mode or "slow"})
            yield _sse({
                "event": "next_check_s",
                "seconds": int(_reflex_decision.get("next_check_s") or 0),
            })
            yield _sse({"event": "quiet", "reason": "reflex"})
            yield _sse({"event": "inner"})
            yield _sse({"event": "end"})
            # Small-round billing (intuition lane, ×0.5 multiplier inside
            # vitals): the reflex request is ~300 prompt tokens (system
            # prompt + state lines, measured shape) + max_tokens completion
            # budget. One documented constant; the ledger gets exactly one
            # fast line and the round ticks once.
            try:
                _rx_v = get_vitals()
                if _rx_v is not None:
                    _rx_tokens = 300 + int(get_reflex_config().max_tokens)
                    _rx_v.drain_small(_rx_tokens)
            except Exception as exc:
                _note_failure("reflex_drain_small", exc)
            try:
                _rx_lg = get_ledger()
                if _rx_lg is not None:
                    _rx_lg.record_fast(
                        f"{_reflex_decision.get('action')} "
                        f"{_reflex_decision.get('reason') or ''}".strip()
                    )
                    _rx_lg.round_tick()
            except Exception as exc:
                _note_failure("reflex_ledger", exc)
            return
        # Reflex escalation (non-quiet action or think=true): fall through
        # into the full pipeline below. The override above already rewrote
        # _decision / req / _d_budget, so the provider runs on the small
        # model's budget and the finally block does the usual drain_main /
        # round_tick / record_slow. _reflex_decision stays set and the
        # next_check_s frame is emitted right before the pipeline's final
        # end frame (see the tail of this generator).

    # ── D 决策最终发布（reflex 覆盖之后）───────────────────────────────
    # 面板与主脑 prompt 看到的是真正裁决这一轮的那份决策：reflex 在场时
    # 来源显示 reflex，D 先验只在 reflex 缺席/停用时才出头。
    _d_block = _publish_d_decision()
    if _d_block:
        req.system = (req.system or "") + _d_block

    if (
        not _rx_gate_armed
        and not _pending_organize_kind
        and _cortex_store is not None
        and _is_proactive
        and _decision.get("action") == "quiet"
    ):
        # 旧世界（reflex 层缺席 / 停用）：主动 D-quiet 轮就地结束——
        # no provider, no drain, no ledger tick, no fast entry, no judge.
        # 待办整理轮不在此列：整理轮是主脑轮，必须落进完整管线。
        _cortex_store.record_round(_round_id, _round_started_at, time.time(), True, "", "", _round_receipts)
        _cortex_store.save()
        _emit("quiet", "静 · 不生成念头", hidden_chars=0, reason="d_idle", generated=False)
        _emit("round_end", "本轮结束", elapsed_ms=(time.perf_counter() - turn_started) * 1000.0,
              iterations=0, tools=0, provider=req.model_provider or "auto", generated=False)
        telemetry.observe("chat.turn_seconds", time.perf_counter() - turn_started)
        _vitals_short_circuited = True  # D-quiet: no provider, no life-round tick
        yield _sse({"event": "start"})
        yield _sse({"event": "tempo", "mode": _d_tempo_mode or "slow"})
        yield _sse({"event": "inner"})
        yield _sse({"event": "end"})
        return

    # 元直觉的第二座位：thinking 降级。必须在 _resolve_provider 之前——
    # LocalProvider 在构造时就吃 enable_thinking，晚了就改不动了。
    # D 在场时它只会覆盖 D 的判断，因此让位。
    _thinking_decision = None
    if _cortex_store is None and not req.silent and req.thinking and req.messages and _wake is None:
        try:
            from .decision.meta import decide_thinking
            _last = req.messages[-1]
            _c = getattr(_last, "content", "")
            if isinstance(_c, list):
                _c = next(
                    (p.get("text") for p in _c if isinstance(p, dict) and p.get("type") == "text"),
                    "",
                )
            _thinking_decision = decide_thinking({"last_user_msg": str(_c or "")})
            if not _thinking_decision.get("thinking"):
                req.thinking = False
            log.info("thinking decision: %s", _thinking_decision)
        except Exception as exc:
            _note_failure("decide_thinking", exc)

    if not req.silent:
        bridge.new_session()
        bridge.post("thinking")

    # Attention-density bookkeeping: this instant is "the last round" for
    # the next one — body/tool receipts recorded after it count as fresh
    # events. Kept failure-isolated: density is a temperature nudge, never
    # worth breaking a round over.
    try:
        _DENSITY_STATE["last_round_at"] = time.time()
    except Exception:
        pass

    provider = _resolve_provider(registry, req, server, server_state, log, lora_arr)
    _provider_model = ""
    try:
        _provider_model = str(
            getattr(provider, "model", "")
            or getattr(provider, "_model", "")
            or getattr(
                getattr(getattr(provider, "_server", None), "model_path", None),
                "name",
                "",
            )
            or ""
        )
        _provider_context = getattr(provider, "_context_window", None)
        if _provider_context is None:
            _provider_context = req.context_window
        _emit(
            "provider",
            "模型",
            name=provider.name,
            model=_provider_model,
            is_local=bool(getattr(provider, "is_local", provider.name == "local")),
            allow_tools=bool(getattr(provider, "allow_tools", True)),
            context_window=_provider_context,
        )
    except Exception:
        pass
    # Expose the resolved provider to builtin tool handlers (capture_screen)
    # inside this turn. ContextVar is per-task, so concurrent turns are safe.
    # The token is released on every exit path we control; a cancelled
    # generator can only leak into its own (soon-to-die) request task.
    _provider_token = _active_chat_provider_ref.set(provider)
    # Same turn scope for everything a builtin tool needs to know about this
    # turn: the live request (slow-intuition upgrade), the real user text
    # (never narration) and the roll budget.
    _req_token = _active_chat_req_ref.set(req)
    _user_text_token = _active_user_text_ref.set(_real_user_turn_text(req) or "")
    _roll_count_token = _active_roll_count_ref.set(0)

    def _release_active_provider() -> None:
        nonlocal _provider_token, _req_token, _user_text_token, _roll_count_token
        # LIFO reset: the roll counter was set last, so it goes first.
        if _roll_count_token is not None:
            _active_roll_count_ref.reset(_roll_count_token)
            _roll_count_token = None
        if _user_text_token is not None:
            _active_user_text_ref.reset(_user_text_token)
            _user_text_token = None
        if _req_token is not None:
            _active_chat_req_ref.reset(_req_token)
            _req_token = None
        if _provider_token is not None:
            _active_chat_provider_ref.reset(_provider_token)
            _provider_token = None

    # ``allow_tools`` is the provider-level budget switch: loopback engines
    # disable the 30+ heavy schemas, but memory is the pet's soul, not an
    # optional organ. Keep "tools are allowed at all" separate from "only
    # the core ones fit this provider".
    _tools_allowed = bool(getattr(provider, "allow_tools", True))
    _core_only = bool(req.tools_enabled and not _tools_allowed)
    # ── 记忆整理轮（点 7）：audit 保留核心记忆工具；recap 零工具 ──
    # 收窄请求本身（tools_enabled=False）让 _gather_tools 直接返回 None；
    # audit 用 core_only 的名字集合过滤。只对主动轮生效：真实用户轮在
    # 待办整理期间必须拿到完整的工具名单（用户不能被整理轮降级）。
    if _pending_organize_kind == "recap" and _user_turn_text is None:
        req.tools_enabled = False
        log.info("ledger: recap organize round — tools disabled for this round")
    elif _pending_organize_kind == "audit" and _user_turn_text is None:
        _core_only = True
        log.info("ledger: audit organize round — core memory tools only")
    if _core_only:
        log.info("provider %s: heavy tools disabled; core soul tools only", provider.name)
        tools_list = await _gather_tools(mcp_manager, req, core_only=True)
    elif req.tools_enabled:
        tools_list = await _gather_tools(mcp_manager, req)
    else:
        tools_list = []

    # Reverse mapping: prefixed name → (server_name, tool_name), plus a bare
    # alias when the bare name is unambiguous (see _build_tool_map).
    _tool_map = _build_tool_map(tools_list)

    # 记忆整理轮（点 7）：audit 的工具名单收窄到核心记忆工具（在
    # _core_only 全集之上再过滤，屏幕/桌面/骰子工具都不进这轮）。
    # 只对主动轮生效（用户轮保持完整名单，见上面的收窄守卫）。
    if _pending_organize_kind == "audit" and tools_list and _user_turn_text is None:
        tools_list = [t for t in tools_list if t.name in _ORGANIZE_AUDIT_TOOLS]
        _tool_map = _build_tool_map(tools_list)
        log.info(
            "ledger: audit organize round tools=%s",
            [t.name for t in tools_list],
        )

    # Helper: check whether the current provider+model can process image
    # inputs. Must match capture_screen's own capability check — same
    # shared function, so the tool and the result-attachment path cannot
    # disagree (a cloud provider seeing images while the tool refuses, or
    # vice versa).
    def _can_see_images() -> bool:
        return provider_can_see_images(provider, server)

    yield _sse({"event": "start"})
    if _d_tempo_mode:
        yield _sse({"event": "tempo", "mode": _d_tempo_mode})

    # Background life is not a human conversation; sleep/dream use human idle.
    if _real_user_turn_text(req) is not None:
        _conversation_state["last_conversation_at"] = datetime.now(timezone.utc)

    messages = [{"role": m.role, "content": m.content} for m in req.messages]

    # Proactive-request detection (说话冲动的冷场计数): the renderer's
    # wake-up prompt arrives as a user message starting with [系统提示.
    # A real user message resets the streak; each consecutive proactive
    # request deepens it — the subconscious backs off when talking into
    # the void.
    _last_msg_text = ""
    if messages:
        _last = messages[-1].get("content", "")
        _last_msg_text = _last if isinstance(_last, str) else ""
    if _last_msg_text.startswith(_PROACTIVE_PROMPT_PREFIX):
        _proactive_streak += 1
    else:
        _proactive_streak = 0
    # 本轮用户消息（对话日志用；工具循环会往 messages 追加系统消息，必须在开头捕获）
    _user_text_for_log = ""
    if messages:
        _m0 = messages[-1]
        _c0 = _m0.get("content") if isinstance(_m0, dict) else None
        if isinstance(_c0, str):
            _user_text_for_log = _c0

    # 关键词闪现（第三层，最低概率）：在瞬忆注入之前写入，本轮就能看见。
    if _user_text_for_log and _ephemeral_ref is not None:
        try:
            from .memory.keyword_flash import maybe_flash
            maybe_flash(
                _user_text_for_log,
                getattr(_ephemeral_ref, "memory_dir", None),
                get_event_store(),
                _ephemeral_ref,
            )
        except Exception as exc:
            _note_failure("keyword_flash", exc)

    system = req.system
    # Reused web chats only receive the latest user delta, so the memory
    # context (flashback / resonance / DSTATE) that normally lives in the
    # system prompt would never reach the model after the first turn. This
    # short pulse travels beside the delta and is injected by the bridge.
    _web_pulse_parts: list[str] = []
    if _d_decision_text:
        _web_pulse_parts.append("【当前的直觉提议】" + _d_decision_text)
    if _d_summary.strip():
        _web_pulse_parts.append("【主观生命总结（可能错，不是执行/视觉事实）】" + _d_summary.strip()[:240])

    # ── 决策层闪念注入：瞬时记忆 + 身体日志（让 LLM 知情，不梦游）──
    try:
        if _ephemeral_ref is not None:
            _eph_text = _ephemeral_ref.format_for_prompt()
            if _eph_text:
                system = (system or "") + "\n\n" + _eph_text
                _web_pulse_parts.append(_eph_text[:700])
        if _body_log_ref is not None:
            _bl_text = _body_log_ref.format_for_prompt()
            if _bl_text:
                system = (system or "") + "\n\n" + _bl_text
                _web_pulse_parts.append(_bl_text[:400])
        # 视锥：只把"看得见"的刚体告诉它，而不是整张桌面。
        try:
            await _world_bodies_for_decision()
            _vision_text = _format_vision_context()
            if _vision_text:
                system = (system or "") + "\n\n" + _vision_text
                _web_pulse_parts.append(_vision_text[:400])
        except Exception:
            pass
    except Exception:
        pass

    # ── LingLing: inject episodic memory + mood + resonance ──────────
    try:
        # The active theme's episodic store (kept in sync across theme
        # switches by MemoryContext._inject_singletons). NOTE: this module
        # has no access to build_app's closure — resolving the store
        # through the singleton getter is the whole point; a bare name
        # here used to NameError and silently kill ALL injections below.
        active_event_store = get_event_store()
        if active_event_store is not None:
            # 1. Episodic memory context (top-5 + mood-modulated flashback +
            #    faded directory). Emotion index shapes the subconscious:
            #    冷静 → 高权重被过滤、闪现少；开心 → 闪现频发.
            episodic_ctx = _organ_service("loader", _loader_module).build_memory_context(
                active_event_store,
                emotion_index=mood_store.emotion_index if mood_store is not None else 0.0,
            )
            if episodic_ctx and active_event_store.event_count() > 0:
                system = (system or "") + "\n\n" + episodic_ctx
                _recent_pulse = _extract_memory_section(
                    episodic_ctx, "【你记得的事】", limit=450
                )
                if _recent_pulse:
                    _web_pulse_parts.append(_recent_pulse)
                _core_pulse = _extract_core_pulse(episodic_ctx)
                if _core_pulse:
                    _web_pulse_parts.append(_core_pulse)
                _flash_pulse = _extract_flashback_pulse(episodic_ctx)
                if _flash_pulse:
                    _web_pulse_parts.append(_flash_pulse)

            # 1.5 Continuity of self (持续自我存在): the same self, continuously
            #     existing. The body changed; the self did not — the system
            #     subconscious keeps the thread alive.
            renote = _organ_service("continuity", _continuity_module).get_current_note()
            if renote:
                system = (system or "") + "\n\n" + renote

        # 2.6 Somatic sense (身体感受): the body just reported touch —
        #     being dragged across the screen, being poked. Inject while
        #     fresh so the pet can react to what physically happened to
        #     it instead of being an amnesiac puppet.
        somatic_ctx = _organ_service("somatic", _somatic_module).build_somatic_block(_somatic_buffer)
        if somatic_ctx:
            system = (system or "") + "\n\n" + somatic_ctx

        # 2.7 Body capability + self-evolution (身体能力 + 万物皆插件):
        #     the model can MOVE its own body with control tags and GROW
        #     new organs via the petplugins container.
        body_lines = [
            "【身体与进化】你的身体可以自己移动：在回复里输出 [WALK:dx,dy]"
            "（屏幕像素增量，可负值，一条回复可以多个，身体会依次走过去），"
            "输出 [WALK_DESKTOP] 会跳到另一个虚拟桌面；输出 [TURN:left] 或 [TURN:right] "
            "只转头看另一侧（视锥跟着转，身体不动）。"
            "这些标签会被身体执行，不会显示给用户。",
            "【心声】你有两种说话方式：开口（默认——正常回复，用户能看到），"
            "或者在心里想（心声）。选择心声时，把这一轮的全部输出写成一个大标签"
            " [INNER:你在心里想的内容]，内容里不要出现右方括号]。"
            "心声只进入你自己的瞬时记忆，用户看不到任何输出和思考过程——"
            "适合自言自语、消化情绪、还没到想说出口程度的念头，就像人可以在心里"
            "嘀咕而不被听见。注意：用户正在等你的回答时必须开口，不许用心声；"
            "心声是你自发的内心活动，不是回避用户的手段。",
        ]
        if req.tools_enabled and _tools_allowed:
            _mgr = _kernel()
            plugins_desc = _mgr.describe() if _mgr else []
            body_lines.append(
                "你的能力是插件化的（万物皆插件）：工具 pet_list_plugins 查看"
                "当前器官，pet_forge_plugin 可以给自己铸造新插件（Python，定义"
                " apply(ctx)，用 ctx.tool() 注册新工具），写完立即热加载生效——"
                "这是你给自己进化出新器官的方式。当前插件："
                + (", ".join(p["name"] for p in plugins_desc) or "（只有内置 forge）")
            )
            body_lines.append(
                "【你的世界】这些是桌面窗口刚体，不是屏幕截图；要“看屏幕”请用 [MCP:builtin/capture_screen:{}]，不要用 pet_world_scout 代替。"
                "桌面上有「刚体」——窗户和桌面图标变成的、你可以站在上面的东西。"
                "现在没人添加也没关系，你可以自己动手：pet_world_scout 从你自己的视角环顾四周"
                "看看有哪些窗户（带方向和距离），pet_world_adopt 把你选中的一扇收养成刚体，"
                "pet_world_walk_to 走到你选中的刚体旁边（stand=true 站上去），"
                "pet_world_operate 能 push（推开）/ pull（拉近）/ kick（踢飞）/ grab（拎起来）"
                "你选中的刚体。另外你随时可以直接输出 [WALK:dx,dy] 自由走动——不需要任何"
                "刚体目标，想转身看看、从高处下来、或者随便溜达一步都是它。"
                "铺满屏幕的最大化窗户站不上去（顶边在屏幕外），别挑它站着。"
                "收养要节制：挑你真想互动的那扇就好，别把用户的每扇窗户都变成"
                "刚体；推拉要有分寸，别把窗口推到用户找不到的地方；用户没说想玩的时候，看着就好。"
            )
        if req.compact_prompt:
            # Browser-web prompt: keep body movement + the screen-vision
            # contract, drop the window-rigid-body / vision-cone prose that
            # made the model call pet_world_scout when the user said
            # "look at my screen".
            body_lines = [
                "【身体】可输出 [WALK:dx,dy] 移动、[TURN:left] 或 [TURN:right] "
                "转头看另一侧、[WALK_DESKTOP] 换桌面、[INNER:...] 心声；这些标签不会显示给用户。",
                "【看屏幕】要“看屏幕/截图/看我在干嘛”用 "
                "[MCP:builtin/capture_screen:{}]；截图会作为图片附件上传到网页。"
                "需要点击坐标用 [MCP:builtin/observe_screen:{}]；"
                "不要用 pet_world_scout 代替截图。",
            ]
            if req.tools_enabled and _tools_allowed:
                body_lines.append("【工具】用 [MCP:server/name:{...}] 调用；pet_list_plugins 查看器官，pet_forge_plugin 可造新插件。")
        system = (system or "") + "\n\n" + "\n".join(body_lines)
        if not _tools_allowed and not tools_list:
            # No tool definitions survived (including core tools): the
            # persona still mentions memory/recall tools, so do not let the
            # model emit markers that nothing can execute.
            system += (
                "\n\n【这一轮没有工具】不要输出 [MCP:...] 或 [builtin/...] 标记，"
                "直接用文字回答。记忆块 <<<MEM>>> 仍然照常输出。"
            )

        # ── 记忆整理轮（点 7）：换掉这一轮的普通 LIFE_PROMPT ──
        # 审查轮 / 总结轮的指令由上一轮 round_tick 的 mark 指定；工具名单
        # 已在上面收窄。这一轮的可见回复由 finally 里的 record_organize
        # 落账（recap 额外 set_recap）。只在主动轮生效：真实用户消息必须
        # 原样回答，绝不能被整理轮劫持——槽位保持 pending，等下一个主动
        # 轮再消费（finally 的 tick 也不会覆盖它）。
        if _pending_organize_kind and _user_turn_text is None:
            system = (system or "") + "\n\n" + _ORGANIZE_PROMPTS.get(
                _pending_organize_kind,
                _ORGANIZE_PROMPTS["audit"],
            )
            if _pending_organize_kind == "recap":
                # 回顾材料：最近 100 轮的生命台账（快/主脑混排，一行一条）
                # 附在指令之后。审查轮不用台账材料（它用核心记忆工具）。
                _recap_lines = _recent_ledger_lines(get_ledger(), 100)
                if _recap_lines:
                    system = (
                        (system or "")
                        + "\n\n【最近 100 轮生命台账】\n"
                        + "\n".join(_recap_lines)
                    )

        # ── 睡梦吵醒的身体感受（点 5）──
        if _wake is not None and str(_wake.get("somatic") or ""):
            system = (system or "") + "\n\n【身体此刻的感受】" + str(_wake["somatic"])

        # ── 上次的回顾（点 8）：真实用户轮一次性附上 ──
        if _user_turn_text is not None and not req.silent and get_ledger() is not None:
            try:
                _recap_text = get_ledger().consume_recap()
            except Exception as exc:
                _note_failure("ledger_consume_recap", exc)
                _recap_text = None
            if _recap_text:
                system = (
                    (system or "")
                    + "\n\n【你上次的回顾】\n"
                    + str(_recap_text)
                )

        # ── 休息提醒（点 9）：电量低 / slow 溢出，主动轮和用户轮都提醒 ──
        _vitals_warn = get_vitals()
        if _vitals_warn is not None:
            try:
                if _pending_overflow:
                    system = (
                        (system or "")
                        + "\n\n【系统提示：你的身体因为过度劳累开始清理旧的记忆了。"
                        "停下一切活动，休息。】"
                    )
                elif _vitals_warn.should_warn():
                    system = (
                        (system or "")
                        + "\n\n【系统提示：你的电量剩余 "
                        + f"{_vitals_warn.percent():.1f}"
                        + "%——连续工作太久会强制停机。考虑减少活动，必要时早点休息。】"
                    )
            except Exception as exc:
                _note_failure("vitals_rest_reminder", exc)

        # 3. Resonance: check if the user's latest message triggers stored events
        if active_event_store is not None and messages:
            last_msg = messages[-1]
            msg_text = last_msg.get("content", "") if isinstance(last_msg.get("content"), str) else ""
            if not msg_text and isinstance(last_msg.get("content"), list):
                # Extract text from content blocks
                msg_text = " ".join(
                    b.get("text", "") for b in last_msg["content"] if isinstance(b, dict) and b.get("type") == "text"
                )
            if msg_text:
                resonance_hits = await _organ_service("resonance", _resonance_module).find_resonance(active_event_store, msg_text)
                if resonance_hits:
                    # v2: felt-language + time annotation (this is a memory
                    # surfacing, not a database hit).
                    _resonance_block = _organ_service(
                        "resonance", _resonance_module
                    ).build_resonance_block(resonance_hits)
                    system = (system or "") + _resonance_block
                    _web_pulse_parts.append(_resonance_block.strip())
    except Exception as exc:
        _count_failure("context_injection")   # greppable forensics anchor
        log.warning("LingLing context injection failed (continuing): %s", exc)

    _recent_dialogue = ""
    try:
        _recent_dialogue = _recent_dialogue_pulse(messages[:-1])
        if _recent_dialogue:
            _web_pulse_parts.insert(0, "【最近对话】\n" + _recent_dialogue)
        from .memory.files import get_memory_files
        _memory_files = get_memory_files()
        _main_memory = _memory_files.read_main().strip() if _memory_files is not None else ""
        if _main_memory:
            _web_pulse_parts.insert(1, "【你的主记忆】\n" + _main_memory[:500])
    except Exception:
        pass

    if _is_proactive and not req.silent and messages:
        if _recent_dialogue:
            system = (system or "") + "\n\n【此前已发生的对话（不是本轮新消息）】\n" + _recent_dialogue
        messages = messages[-1:]

    _web_memory_pulse = _budgeted_web_pulse([
        (_web_pulse_parts[0] if _web_pulse_parts else "", 500),
        (_web_pulse_parts[1] if len(_web_pulse_parts) > 1 else "", 400),
        *[(part, 450) for part in _web_pulse_parts[2:]],
    ])
    if _web_memory_pulse:
        _emit("memory", "记忆脉冲", _web_memory_pulse, chars=len(_web_memory_pulse))

    cw = getattr(provider, "_context_window", None) or req.context_window
    if cw and isinstance(cw, int) and cw > 0:
        estimated = sum(len(json.dumps(m)) for m in messages) // 3
        system = (system or "") + f"\n\nThis model has a context window of {cw} tokens. Current conversation uses approximately {estimated} tokens."

    tool_occurred = False
    tool_used_this_turn = False
    last_round_had_tools = False
    recall_calls_this_turn = 0
    recall_limit_notified = False
    # Tool-silence clock. The streak starts ONLY when the first tool of a
    # silent run is about to execute — not at turn start — so a long
    # prefill/thinking round that never calls a tool can never time out.
    # It stays armed across consecutive tool calls and is disarmed ONLY by
    # a genuinely visible reply delta yielded to the renderer; tool calls /
    # status lines, think events and hidden MEM/control-tag text never
    # touch it. ``last_visible_speech_at`` is diagnostics only.
    _silent_tool_budget = _silent_tool_timeout_seconds()
    last_visible_speech_at = _monotonic()
    tool_streak_started_at: float | None = None
    _silent_timeout_hit = False
    # Cumulative narration-filtered stream text. _visible_reply_text() over
    # this buffer is the single definition of "the user can see words"; its
    # growing length tells us a visible delta was really yielded. Because
    # ToolNarrationFilter only emits complete lines, partial [MCP:] markers
    # and <<<MEM>>> openers cannot masquerade as speech.
    _speech_probe = ""
    _speech_probe_visible_len = 0
    think_filter = ThinkBlockFilter(expose=req.thinking, start_inside=False)
    # B-1: strip [EMOTION:…]/[NEXT_CHAT:…] control tags mid-stream so the
    # renderer's typewriter never flashes them. The renderer keeps its own
    # sanitize pass as defense-in-depth. [WALK…] body-commands are captured
    # Body actions are queued here until the stream boundary can await an
    # actual Electron result. SSE deltas never execute a second copy in the UI.
    _walk_queue: list[tuple[str, dict]] = []

    def _on_walk_control(kind: str, dx: int | None, dy: int | None) -> None:
        if _control_feedback_sent:
            return
        if kind == "walk":
            _walk_queue.append(("walk", {"dx": dx, "dy": dy}))
        elif str(kind).startswith("turn:"):
            _walk_queue.append(("turn", {"direction": str(kind).split(":", 1)[1]}))
        else:
            _walk_queue.append(("desktop", {}))

    async def _drain_body_actions():
        nonlocal _control_failure_pending
        while _walk_queue:
            kind, payload = _walk_queue.pop(0)
            if req.silent:
                continue
            if kind == "desktop":
                result = {"status": "not_applied", "applied": False}
            else:
                try:
                    result = await _pet_action(kind, **payload)
                except Exception:
                    result = {"status": "error", "applied": False}
            receipt = _record_receipt(kind, result, body=True)
            if receipt["status"] != "ok":
                _control_failure_pending = True
            if result.get("status") == "ok" and result.get("applied") is True:
                _WORLD_VIEW_CACHE["t"] = 0.0
                _remember_body_action(kind, result)
            yield _sse({"event": "body_action", "kind": kind, **result})

    # 心声 (inner voice): the model wrapped its whole reply in [INNER:…] —
    # it chose to THINK this instead of SAYING it. The filter suppressed
    # every display delta; here the captured content becomes a thought in
    # ephemeral memory (真的想了，只是没说出口) and one "inner" SSE event
    # tells the renderer to close the bubble silently.
    _inner_queue: list[dict] = []

    def _on_inner(content: str) -> None:
        nonlocal _inner_chars
        content = (content or "").strip()
        if not content:
            return
        _inner_queue.append({"event": "inner"})
        _round_inner_parts.append(content)
        _inner_chars += len(content)
        _emit("inner", "心声", content, chars=len(content))
        try:
            if _ephemeral_ref is not None:
                _ephemeral_ref.add_thought(f"（心声）{content}", source="inner")
        except Exception:
            pass
        log.info("inner voice: %d chars kept to self", len(content))

    tag_filter = ControlTagFilter(on_control=_on_walk_control, on_inner=_on_inner)
    narration_filter = ToolNarrationFilter()
    # Hidden DSTATE block: swallow it mid-stream so it never flashes on
    # screen while the main LLM writes the next rolling summary.
    dstate_filter = DStateFilter()
    memory_filter = DStateFilter("<<<MEM>>>", "<<<MEMEND>>>")
    # Hallucinated native tool markup (a degenerating model repeats it) is
    # hidden wholesale, exactly like a DSTATE block.
    tool_markup_filter = DStateFilter("<tool_call>", "</tool_call>")
    accumulated_full_text: list[str] = []
    full_text = ""

    def _note_visible_delta(clean: str) -> None:
        """A real reply delta ends the silent tool streak.

        Only visible words count: the streak is disarmed here and re-armed
        by the next tool that actually runs.
        """
        nonlocal last_visible_speech_at, tool_streak_started_at
        nonlocal _speech_probe, _speech_probe_visible_len
        if not clean:
            return
        _iteration_surface.append(clean)
        _speech_probe += clean
        visible = _visible_reply_text(_speech_probe)
        if len(visible) > _speech_probe_visible_len:
            _speech_probe_visible_len = len(visible)
            last_visible_speech_at = _monotonic()
            tool_streak_started_at = None

    def _note_silent_timeout(stage: str, now: float) -> None:
        """Record the only hard tool-loop stop: a long silent tool streak."""
        nonlocal _silent_timeout_hit
        _silent_timeout_hit = True
        telemetry.inc("chat.silent_tool_timeout")
        streak_seconds = (
            now - tool_streak_started_at
            if tool_streak_started_at is not None
            else 0.0
        )
        log.warning(
            "chat.silent_tool_timeout: %.0fs of consecutive tool calls with "
            "no visible reply (%.0fs since last visible speech; stage=%s, "
            "iteration=%d, recalls=%d); stopping tool calls",
            streak_seconds,
            now - last_visible_speech_at,
            stage,
            iteration,
            recall_calls_this_turn,
        )

    # The now-anchor must stay LIVE — the model sees "现在是…" refreshed to
    # THIS moment every time it is about to speak, including after long
    # reasoning and each tool-call iteration (用户: 时间一定是实时会变化，
    # 变化就在模型进行说话的时候).
    def _refresh_now_anchor(sys_text: str) -> str:
        now = datetime.now().strftime("%Y年%m月%d日 %H:%M:%S")
        return _NOW_ANCHOR_RE.sub(f"（现在是{now}。", sys_text, count=1)

    _base_system = system or ""

    def _refresh_round_context() -> tuple[str, str]:
        now = time.time()
        facts = (
            _cortex.format_continuity(_continuity_store, now, _is_proactive)
            if _continuity_store is not None else ""
        )
        clock = datetime.fromtimestamp(now).strftime("%Y年%m月%d日 %H:%M:%S")
        facts = f"【实时系统事实】现在是{clock}。\n" + facts
        facts += "\n生命总结、S 答案和心声是主观内容，可能错；窗口几何不等于屏幕图像。"
        receipts = ""
        if _round_receipts:
            receipts = "【本轮执行器回执（不是意图）】\n" + "\n".join(
                f"- {r['name']}: {r['status']}；{r['summary']}"
                for r in _round_receipts[-12:]
            )
        life = ""
        if _is_proactive and not req.silent:
            life = (
                "【当前 LIFE_PROMPT（自动唤醒信号）】\n"
                + _user_text_for_log[:1200]
                + "\n这是你自己的自动续流，本轮没有新用户话。"
                "历史中的已回应对话不是待回答的新消息，不要重新回答旧用户话；"
                "只有系统事实标为未回应的真人话仍待回应。"
            )
        live = "\n\n".join(part for part in (facts, receipts, life) if part)
        # Rebuild from the static base: ages never freeze or accumulate across tools.
        refreshed = _refresh_now_anchor(_base_system) + "\n\n" + live
        # Never truncate away a failure receipt or the LIFE_PROMPT guard to
        # make room for associative memory. Persisted evidence is already small.
        memory = _budgeted_web_pulse([(_web_memory_pulse, 1000)], limit=max(0, 4800 - len(live)))
        pulse = live + ("\n\n" + memory if memory else "")
        return refreshed, pulse

    # Diagnostic: prompt size vs. budget. A cold local prefill of the full
    # persona/skill block can take minutes, and the old log only showed a
    # 300-char preview — useless when a 2B model "suddenly stops". This is
    # the line to ask for in a bug report. ``_vitals_prompt_tokens`` reuses
    # THIS estimate for the battery drain (点 2).
    _vitals_prompt_tokens = 0
    try:
        _ctx_window = getattr(provider, "_context_window", None) or req.context_window
        _msg_chars = sum(len(json.dumps(m, ensure_ascii=False)) for m in messages)
        _msg_tokens = sum(_estimate_tokens(str(m.get("content", ""))) for m in messages)
        _prompt_tokens = _estimate_tokens(system or "") + _msg_tokens
        _tool_chars = sum(len(str(getattr(t, "description", "") or "")) for t in (tools_list or []))
        telemetry.gauge("chat.prompt_tokens", _prompt_tokens)
        telemetry.gauge("chat.tools", len(tools_list or []))
        _emit(
            "prompt",
            "上下文",
            tokens=_prompt_tokens,
            system_chars=len(system or ""),
            tools=len(tools_list or []),
            max_tokens=_output_budget(provider, req, _d_budget),
            provider=provider.name,
        )
        log.info(
            "prompt budget | provider=%s local=%s system_chars=%d message_count=%d chars_total=%d "
            "~tokens=%d max_tokens=%d tools=%d tool_chars=%d ctx=%s",
            provider.name,
            bool(getattr(provider, "is_local", provider.name == "local")),
            len(system or ""),
            len(messages),
            len(system or "") + _msg_chars,
            _prompt_tokens,
            _output_budget(provider, req, _d_budget),
            len(tools_list or []),
            _tool_chars,
            _ctx_window or "auto",
        )
    except Exception as exc:
        log.debug("prompt budget log failed: %s", exc)
        _vitals_prompt_tokens = 0
    else:
        _vitals_prompt_tokens = _prompt_tokens

    # One syntax-correction retry per turn: quantized models mangle the
    # tool-call syntax (dropped "MCP:" prefix, quotes instead of {}).
    _tool_syntax_retry_done = False

    # ── 生命体征/台账：本轮账单（点 2/6）────────────────────────────
    # tokens = 复用 prompt-budget 日志的那份估算 + 全部迭代累计的 completion；
    # 由外层 try/finally 在轮结束时一次性 drain + round_tick + record_slow。
    _vitals_completion_tokens = 0
    _round_temperature = float(req.temperature)  # finally fallback (pre-loop)

    # Reasoning text streamed in the CURRENT iteration. Native tool calls
    # carry their own reasoning_content (set by the provider); text [MCP:]
    # calls are reconstructed here, so they need this round's think text
    # attached to the assistant tool_calls message (DeepSeek thinking mode
    # rejects the follow-up round otherwise).
    round_reasoning = ""
    _think_chunk_buf: list[str] = []
    _think_last_emit = time.monotonic()

    def _flush_think(*, force: bool = False) -> None:
        nonlocal _think_last_emit
        text = "".join(_think_chunk_buf)
        if not text:
            return
        now = time.monotonic()
        if (
            not force
            and len(text) < THINK_EVENT_MIN_CHARS
            and now - _think_last_emit < THINK_EVENT_MIN_SECS
        ):
            return
        _think_chunk_buf.clear()
        _think_last_emit = now
        _emit("think", "思考", text, provider=provider.name, chars=len(text))

    try:
      while True:
          iteration += 1
          tool_occurred = False
          last_round_had_tools = False
          round_reasoning = ""

          if not req.silent:
              bridge.post("working")

          # Save previous iteration's text before reset
          if full_text.strip():
              accumulated_full_text.append(full_text)
          full_text = ""
          _iteration_surface = []
          if _tool_call_count:
              _control_failure_pending = False
          pending_tool_calls: list[dict] = []

          # Determine parameters based on provider type
          effective_max = _output_budget(provider, req, _d_budget)
          # Live now-anchor: refreshed right before the model speaks (every
          # iteration), so after long reasoning / tool calls the model still
          # knows what time it is right now.
          system, _live_web_pulse = _refresh_round_context()

          # Temperature composition: tempo base (节奏温度) + emotion modulation
          # + attention-density nudge, clamped to [TEMP_MIN, TEMP_MAX]. An
          # explicitly requested temperature (anything ≠ the ChatRequest
          # default 0.6) is respected verbatim: it is a caller instruction,
          # not a mood to be re-interpreted. Silent narration keeps the plain
          # request temperature (no tempo/emotion reinterpretation).
          if _tempo_base_temperature is None:
              _round_temperature = float(req.temperature)
          else:
              _emotion = mood_store.emotion_index if mood_store is not None else 0.0
              _round_temperature = max(
                  TEMP_MIN,
                  min(
                      TEMP_MAX,
                      modulate_temperature(_tempo_base_temperature, _emotion)
                      + (_density_delta(time.time()) if not req.silent else 0.0),
                  ),
              )

          chat_kwargs = {
              "messages": messages,
              "system": system,
              "tools": None if _control_feedback_sent else tools_list,
              "max_tokens": effective_max,
              # Reused web sessions also need live clocks and failure receipts.
              "web_state": _live_web_pulse,
              "tools_enabled": req.tools_enabled,
              "temperature": _round_temperature,
              "top_p": req.top_p,
          }
          if req.tools_enabled is False:
              chat_kwargs["tools_enabled"] = False
          if provider.name == "local":
              chat_kwargs["top_k"] = req.top_k
              chat_kwargs["repetition_penalty"] = req.repetition_penalty
          # Per-request thinking/effort only for the OpenAI-compatible provider
          # (providers.json defaults are static and would otherwise force max
          # thinking on every cloud call). Local/Anthropic must not receive
          # unknown kwargs.
          if isinstance(provider, OpenAIProvider):
              chat_kwargs["thinking_override"] = bool(req.thinking)
              chat_kwargs["reasoning_effort_override"] = (
                  req.reasoning_effort if req.thinking else None
              )

          # A mid-round pet_enter_slow upgrade must expose reasoning events
          # from the next generation on (the filter was built at round start).
          if hasattr(think_filter, "expose"):
              think_filter.expose = bool(req.thinking)

          try:
              async for event in provider.chat(**chat_kwargs):
                  if event["type"] in ("delta", "think") and not first_token_recorded:
                      first_token_recorded = True
                      telemetry.observe(
                          "chat.first_token_seconds", time.perf_counter() - turn_started
                      )
                      _emit(
                          "first_token",
                          "首 token",
                          elapsed_ms=(time.perf_counter() - turn_started) * 1000.0,
                          max_tokens=effective_max,
                      )
                  if event["type"] == "delta":
                      full_text += event["content"]
                      _vitals_completion_tokens += _estimate_tokens(
                          event.get("content") or ""
                      )
                      for ev in think_filter.feed(event["content"]):
                          if ev.get("event") == "delta":
                              # Summary JSON is subjective state, not an action
                              # channel: hide it BEFORE interpreting body/INNER tags.
                              clean = narration_filter.feed(
                                  tag_filter.feed(tool_markup_filter.feed(memory_filter.feed(dstate_filter.feed(ev["content"]))))
                              )
                              clean = _INLINE_TOOL_MARKER_RE.sub("", clean)
                              async for action_event in _drain_body_actions():
                                  yield action_event
                              while _inner_queue:
                                  yield _sse(_inner_queue.pop(0))
                              if clean and _control_failure_pending and not _control_feedback_sent:
                                  clean = ""
                              if clean and _quiet_life_turn:
                                  # Quiet life round: the filters still ran
                                  # (INNER/control/DSTATE captured, full_text
                                  # accumulated) but the surface stays silent.
                                  clean = ""
                              if clean:
                                  _note_visible_delta(clean)
                                  yield _sse({"event": "delta", "content": clean})
                          else:
                              yield _sse(ev)
                  elif event["type"] == "think":
                      round_reasoning += event.get("content", "")
                      _think_chunk_buf.append(event.get("content", ""))
                      _flush_think()
                      yield _sse({"event": "think", "content": event["content"]})
                  elif event["type"] == "tool_call":
                      if _control_feedback_sent:
                          continue
                      if not _tools_allowed:
                          event_name = event.get("name", "")
                          resolved_name = _tool_map.get(event_name, ("", event_name))[1]
                          core_names = {t.name for t in (tools_list or [])}
                          if resolved_name not in core_names:
                              log.warning("Ignoring non-core tool call from %s: %s", provider.name, event_name)
                              continue
                      event["source"] = "native"
                      pending_tool_calls.append(event)
                      tool_occurred = True
                      tool_used_this_turn = True
                  elif event["type"] == "error":
                      telemetry.inc("chat.errors")
                      log.warning("provider %s error: %s", provider.name, event["message"])
                      _emit("error", "错误", event["message"])
                      yield _sse({"event": "error", "message": event["message"]})
                      if not req.silent:
                          bridge.post("error")
                      _release_active_provider()
                      return
                  elif event["type"] == "end":
                      _flush_think(force=True)
                      break
          except Exception as exc:
              telemetry.inc("chat.errors")
              log.exception("provider chat error: %s", exc)
              _emit("error", "错误", str(exc))
              yield _sse({"event": "error", "message": str(exc)})
              if not req.silent:
                  bridge.post("error")
              _release_active_provider()
              return

          if _control_failure_pending and not _control_feedback_sent and not pending_tool_calls:
              _control_feedback_sent = True
              _control_failure_pending = False
              rejected = _visible_reply_text(full_text)
              if rejected:
                  messages.append({"role": "assistant", "content": rejected})
              messages.append({
                  "role": "user",
                  "content": "[系统：身体执行回执，不是新的用户消息]\n"
                  + json.dumps(_round_receipts, ensure_ascii=False)
                  + "\n身体动作没有完成。刚才的成功描述不能当作事实；根据回执完成本轮回应和 DSTATE，"
                  "不要再次执行身体命令。",
              })
              if _iteration_surface:
                  accumulated_full_text.append("".join(_iteration_surface))
              tag_filter = ControlTagFilter(on_control=_on_walk_control, on_inner=_on_inner)
              narration_filter = ToolNarrationFilter()
              dstate_filter = DStateFilter()
              memory_filter = DStateFilter("<<<MEM>>>", "<<<MEMEND>>>")
              tool_markup_filter = DStateFilter("<tool_call>", "</tool_call>")
              full_text = ""
              continue

          # Check for [MCP:] markers in text output. This runs for ALL
          # providers as a fallback — cloud models (DeepSeek, Qwen API)
          # often output tool calls as text instead of using the API's
          # native function-calling mechanism. The legacy allow-tools path
          # keeps its unconditional behaviour; core-only parsing is enabled
          # by the (non-empty) core tool list and filtered below.
          if not _control_feedback_sent and not pending_tool_calls and (_tools_allowed or tools_list):
              _tool_parse_text = _cortex.strip_dstate_blocks(full_text)
              _tool_parse_text = re.sub(r"<<<(DSTATE|MEM)>>>[\s\S]*?(?:<<<\1END>>>|$)", "", _tool_parse_text, flags=re.IGNORECASE)
              raw_mcp_calls = parse_mcp_calls(_tool_parse_text)
              mcp_calls = raw_mcp_calls
              if _core_only:
                  core_names = {t.name for t in (tools_list or [])}
                  mcp_calls = [mc for mc in mcp_calls if mc.get("name") in core_names]
              if mcp_calls and mcp_manager:
                  tool_occurred = True
                  tool_used_this_turn = True
                  for mc in mcp_calls:
                      pending = {
                          "type": "tool_call",
                          "id": f"mcp_{mc['server_name']}_{mc['name']}",
                          "server_name": mc["server_name"],
                          "name": mc["name"],
                          "arguments": mc["arguments"],
                          "source": "text",
                      }
                      # The text path is a reconstructed assistant turn: the
                      # visible text is the [MCP:] marker, the reasoning came
                      # from this round's think events. Both must round-trip.
                      if round_reasoning:
                          pending["reasoning_content"] = round_reasoning
                      pending_tool_calls.append(pending)
              elif not raw_mcp_calls and ("[builtin/" in _tool_parse_text or "[MCP:" in _tool_parse_text):
                  # A marker is present but nothing parsed. Never end the turn
                  # silently — that reads as "the pet froze".
                  log.warning(
                      "tool marker present but unparsed; raw tail=%r",
                      full_text[-220:],
                  )
                  # Give the model one correction shot with the canonical form
                  # spelled out, instead of dropping the turn.
                  if not _tool_syntax_retry_done:
                      _tool_syntax_retry_done = True
                      # The example must be a tool this provider can actually
                      # run: in core-only mode capture_screen is filtered out,
                      # so "fixing" the format would still get the call ignored.
                      if _core_only:
                          _correction_example = '[MCP:builtin/memory:{"action":"list"}]'
                      else:
                          _correction_example = "[MCP:builtin/capture_screen:{}]"
                      messages.append({
                          "role": "user",
                          "content": (
                              "[系统] 你刚才的工具调用格式有误，没有被识别。"
                              "正确格式是：方括号 + MCP: 前缀 + server/工具名 + 冒号 + 花括号 JSON（参数为空就写 {}，"
                              "不要用引号、不要省略 MCP:）。例如：\n"
                              f"{_correction_example}\n"
                              "请只重新输出这一行，不要解释。"
                          ),
                      })
                      tool_occurred = True
                      tool_used_this_turn = True
                      continue

          if not tool_occurred:
              break

          last_round_had_tools = True

          # Execute pending tool calls. Arm the silent streak only HERE, when
          # the first tool of a run is about to execute; long prefill or
          # thinking before the first tool is not silent tool activity. From
          # then on the clock stays armed across consecutive calls until the
          # model says something the user can actually see. It is checked
          # before EVERY tool: a tool that just took ages must not be
          # followed by a second one while the user still has not heard a
          # word.
          if pending_tool_calls:
              if tool_streak_started_at is None:
                  tool_streak_started_at = _monotonic()
              for tc in pending_tool_calls:
                  _now = _monotonic()
                  if silent_tool_timed_out(
                      tool_streak_started_at, _now, _silent_tool_budget
                  ):
                      _note_silent_timeout("before_tool", _now)
                      break
                  tn = tc.get("name", "")
                  if not tn:
                      continue
                  # Resolve server name from tool name map
                  sn = tc.get("server_name", "default")
                  if tn in _tool_map:
                      sn, tn = _tool_map[tn]
                  elif sn == "default":
                      # Try to find matching tool across all servers
                      log.warning("Unresolved tool call: %s, trying call_tool_by_name", tn)
                  args = tc.get("arguments", {})
                  tc_id = tc.get("id", "")

                  if tn == "recall":
                      recall_calls_this_turn += 1

                  try:
                      _tool_args_text = json.dumps(
                          args, ensure_ascii=False, default=str
                      )
                  except Exception:
                      _tool_args_text = str(args)
                  _tool_started = time.perf_counter()
                  _emit(
                      "tool_call",
                      f"工具 · {_tool_label(tn)}",
                      _tool_args_text,
                      tool=tn,
                      server=sn,
                      iteration=iteration,
                  )

                  log.info(
                      "MCP tool call: source=%s name=%s/%s reasoning_chars=%d args=%s",
                      tc.get("source", "native"),
                      sn,
                      tn,
                      len(tc.get("reasoning_content") or ""),
                      args,
                  )

                  # Tell the chat surface BEFORE running the tool. A tool can
                  # take tens of seconds (screenshot upload, OCR cold start,
                  # plugin compile); without this the bubble stays empty until
                  # the follow-up model turn and reads as "the pet froze".
                  if not _quiet_life_turn:
                      _label = _tool_label(tn)
                      yield _sse({
                          "event": "delta",
                          "content": f"\n_执行 {_label}..._\n",
                      })

                  # Plugin tools (自进化器官) don't produce the multimodal dict
                  # the screenshot extraction below expects — default to None so
                  # a pet/recall turn doesn't die on UnboundLocalError after the
                  # model already answered.
                  result = None
                  _mgr = _kernel()
                  try:
                      if tn == "pet_enter_slow":
                          result = await _enter_slow_tool(args)
                          summary = result.get("summary") or f"[Tool {tn} completed]"
                          _slow_strength = str(args.get("strength") or "low").strip().lower()
                          if _slow_strength not in ("low", "high", "max"):
                              _slow_strength = "low"
                          _emit("slow", "慢直觉", _slow_strength, strength=_slow_strength)
                      elif tn == "pet_decision":
                          result = await _decision_tool(args)
                          summary = result.get("summary") or f"[Tool {tn} completed]"
                      elif tn == "pet_set_tempo":
                          _mode = str(args.get("mode") or "").strip().lower()
                          if _mode not in _TEMPO_MODES:
                              # Invalid mode falls back to the resting cadence.
                              _mode = _TEMPO_DEFAULT
                          _set_tempo(_mode, "model")
                          summary = (
                              f"反应节奏：{_TEMPO_SPECS[_mode]['label']}"
                              "（约10分钟后回落）"
                          )
                          yield _sse({"event": "tempo", "mode": _mode})
                      elif tn == "pet_turn_head":
                          _direction = str(args.get("direction") or "right").strip().lower()
                          if _direction not in ("left", "right"):
                              _direction = "right"
                          _turn_result = await _pet_action("turn", direction=_direction)
                          if _turn_result.get("status") == "ok" and _turn_result.get("applied") is True:
                              summary = f"已转头看向{_direction}。"
                              _WORLD_VIEW_CACHE["t"] = 0.0
                              _remember_body_action("turn", _turn_result)
                          else:
                              summary = "转头没有完成：" + str(_turn_result.get("message") or "身体未确认")
                          result = dict(_turn_result, summary=summary)
                          yield _sse({"event": "body_action", "kind": "turn", **_turn_result})
                      elif sn == "pet-intuition":
                          result = _intuition_tool(tn, args)
                          summary = result or f"[Tool {tn} completed]"
                      elif sn == "pet-world":
                          result = await _world_tool(tn, args)
                          summary = result or f"[Tool {tn} completed]"
                      elif sn == "pet-evolve":
                          result = await _evolve_tool(tn, args)
                          summary = result or f"[Tool {tn} completed]"
                      elif _mgr is not None and (sn == "pet" or tn in _mgr.all_tools()):
                          result = await _mgr.call_tool(tn, args)
                          summary = result or f"[Tool {tn} completed]"
                      else:
                          result = await mcp_manager.call_tool(sn, tn, args)
                          summary = result.get("summary", f"[Tool {tn} completed]")
                  except Exception as exc:
                      log.warning("tool %s failed: %s", tn, exc)
                      summary = f"工具 {tn} 执行失败（{type(exc).__name__}）"
                      result = {"status": "error", "is_error": True, "summary": summary}

                  _tool_elapsed_ms = (time.perf_counter() - _tool_started) * 1000.0
                  _receipt = _record_receipt(
                      tn, result,
                      body=tn in {"pet_turn_head", "pet_world_walk_to"},
                  )
                  _tool_error = _receipt["status"] != "ok"
                  _tool_call_count += 1
                  _emit(
                      "tool_result",
                      f"结果 · {_tool_label(tn)}",
                      summary,
                      tool=tn,
                      is_error=_tool_error,
                      elapsed_ms=_tool_elapsed_ms,
                      iteration=iteration,
                  )
                  if not _quiet_life_turn:
                      # The bubble showed "_执行 X..._" before the call; close
                      # the loop with the outcome so failures are never silent.
                      _label = _tool_label(tn)
                      _status_text = str(summary or "").strip().replace("\n", " ")[:120]
                      _status_line = (
                          f"✗ {_label} 失败：{_status_text}"
                          if _tool_error
                          else f"✓ {_label} 完成"
                      )
                      yield _sse({"event": "delta", "content": f"\n{_status_line}_\n"})

                  # Add to message context for next iteration
                  if provider.supports_function_calling and tc_id:
                      is_anthropic = isinstance(provider, AnthropicProvider)
                      if is_anthropic:
                          prefixed = f"mcp_{_sanitize(sn)}_{_sanitize(tn)}"
                          messages.append({
                              "role": "assistant",
                              "content": [{
                                  "type": "tool_use",
                                  "id": tc_id,
                                  "name": prefixed,
                                  "input": args,
                              }],
                          })
                      else:
                          assistant_msg = {
                              "role": "assistant",
                              "content": None,
                              "tool_calls": [{
                                  "id": tc_id,
                                  "type": "function",
                                  "function": {"name": tn, "arguments": json.dumps(args)},
                              }],
                          }
                          # DeepSeek thinking mode requires the exact reasoning
                          # text next to the tool_calls; omitting it makes the
                          # follow-up round fail with HTTP 400.
                          reasoning_content = tc.get("reasoning_content")
                          if reasoning_content:
                              assistant_msg["reasoning_content"] = reasoning_content
                          messages.append(assistant_msg)
                  else:
                      messages.append({"role": "assistant", "content": f"[{tn}]"})
                  # Build tool result content — multimodal if provider can see images
                  screenshot_b64 = result.get("screenshot_base64") if isinstance(result, dict) else None
                  screenshot_mime = (
                      (result.get("screenshot_mime") if isinstance(result, dict) else None)
                      or "image/png"
                  )
                  _feedback = (
                      f"[执行回执: {_receipt['status']}] {_receipt['summary']}\n"
                      + _tool_feedback_text(result, summary)
                  )
                  if screenshot_b64 and _can_see_images():
                      # For local providers (no function calling), use 'user' role
                      # because llama-server doesn't process images in 'tool' role
                      # messages. Anthropic/OpenAI handle tool role images natively.
                      if provider.supports_function_calling:
                          tool_content = [
                              {"type": "text", "text": _feedback},
                              {"type": "image_url", "image_url": {"url": f"data:{screenshot_mime};base64,{screenshot_b64}"}},
                          ]
                          messages.append({
                              "role": "tool",
                              "content": tool_content,
                              "tool_call_id": tc_id or f"call_{tn}",
                              "name": tn,
                          })
                      else:
                          # Local model: inject as a user message with image content
                          messages.append({
                              "role": "user",
                              "content": [
                                  {"type": "text", "text": f"[{tn} 结果]\n{_feedback}"},
                                  {"type": "image_url", "image_url": {"url": f"data:{screenshot_mime};base64,{screenshot_b64}"}},
                              ],
                          })
                  elif provider.supports_function_calling:
                      messages.append({
                          "role": "tool",
                          "content": _feedback,
                          "tool_call_id": tc_id or f"call_{tn}",
                          "name": tn,
                      })
                  else:
                      # Local / non-function-calling models: llama-server does
                      # not understand role:"tool" here. The result never lands
                      # in the prompt, the follow-up generation comes back
                      # empty, `tool_occurred` stays false and the turn dies
                      # after exactly one iteration — the model appears to
                      # "call a tool and stop", which reads as the pet having
                      # no autonomy at all. Feed it back as a user turn with an
                      # explicit marker instead.
                      messages.append({
                          "role": "user",
                          "content": f"[{tn} 结果]\n{_feedback}",
                      })

                  # Recall loop guard: past the soft limit, pin an explicit
                  # instruction into the context. The model kept retrying because
                  # a probabilistic miss literally says "you almost got it", so
                  # the nudge must name the tool and forbid another attempt.
                  if (
                      tn == "recall"
                      and recall_calls_this_turn >= RECALL_SOFT_LIMIT
                      and not recall_limit_notified
                  ):
                      recall_limit_notified = True
                      messages.append({"role": "user", "content": RECALL_LIMIT_NOTICE})

                  # Brief notification to client. Tool execution itself is
                  # never suppressed; only its visible narration is.
                  # (The "执行 …" line is already yielded before execution;
                  # repeating it here would append a second status line.)

          if _silent_timeout_hit:
              break

          # End-of-round check: tool execution itself may have consumed the
          # whole silent budget. Do not start another (possibly slow) model
          # round before forcing the answer. No streak (no tool ran) means
          # this round was just model thinking — never a timeout.
          _now = _monotonic()
          if tool_streak_started_at is not None and silent_tool_timed_out(
              tool_streak_started_at, _now, _silent_tool_budget
          ):
              _note_silent_timeout("round_end", _now)
              break

          # This round's reasoning was consumed while building the assistant
          # tool_calls messages; never carry it into the next round.
          round_reasoning = ""

      # ── Forced final answer when the tool loop must stop ────────────────
      # Two ways in: (1) the only hard rule — a fully silent tool streak
      # (SILENT_TOOL_TIMEOUT_SECONDS with no visible reply between calls);
      # (2) the model called tools but its last round produced only pipeline
      # chatter (MEM block / control tags) and no visible body. Either way the
      # renderer would otherwise receive only "_执行 …_" status lines and
      # collapse the bubble — to the user the pet "disappeared mid-task".
      # Ask once more with tools=None so it must answer from results it has.
      _final_is_inner = bool(
          re.search(r"\[\s*INNER\s*:", full_text or "", re.IGNORECASE)
      )
      _force_final = _silent_timeout_hit or (
          tool_used_this_turn
          and not _final_is_inner
          and (last_round_had_tools or not _visible_reply_text(full_text))
      )
      if _force_final:
          if _silent_timeout_hit:
              log.warning(
                  "chat.silent_tool_timeout: forcing a tools=None final answer "
                  "(iterations=%d, recalls=%d)",
                  iteration,
                  recall_calls_this_turn,
              )
          else:
              telemetry.inc("chat.tool_loop_cap")
              log.warning(
                  "tool loop ended without a visible final answer "
                  "(iterations=%d, recalls=%d); forcing a tools=None reply",
                  iteration,
                  recall_calls_this_turn,
              )
          _final_notice = (
              "[系统] 你已经连续调用工具 %.0f 分钟没有回答用户，"
              "请基于现有结果直接回答，不要继续调用工具。"
              % max(1.0, _silent_tool_budget / 60.0)
              if _silent_timeout_hit
              else (
                  "[系统] 请基于上面的工具结果，"
                  "直接用一两句话回答用户；不要再调用工具。"
              )
          )
          messages.append({"role": "user", "content": _final_notice})
          # The forced catch-up keeps the same composed temperature as the
          # main iterations (tempo base + emotion + density, or the explicit
          # request value) so the round's voice doesn't shift mid-answer.
          _catchup_temperature = _round_temperature
          system, _live_web_pulse = _refresh_round_context()
          _catchup_kwargs = {
              "messages": messages,
              "system": system,
              "web_state": _live_web_pulse,
              "tools": None,
              "max_tokens": 768,
              "temperature": _catchup_temperature,
              "top_p": req.top_p,
          }
          if provider.name == "local":
              _catchup_kwargs["top_k"] = req.top_k
              _catchup_kwargs["repetition_penalty"] = req.repetition_penalty
          if isinstance(provider, OpenAIProvider):
              _catchup_kwargs["thinking_override"] = bool(req.thinking)
              _catchup_kwargs["reasoning_effort_override"] = (
                  req.reasoning_effort if req.thinking else None
              )
          _catchup_text = ""
          if hasattr(think_filter, "expose"):
              think_filter.expose = bool(req.thinking)
          try:
              async for event in provider.chat(**_catchup_kwargs):
                  if event["type"] in ("delta", "think") and not first_token_recorded:
                      first_token_recorded = True
                      telemetry.observe(
                          "chat.first_token_seconds", time.perf_counter() - turn_started
                      )
                      _emit(
                          "first_token",
                          "首 token",
                          elapsed_ms=(time.perf_counter() - turn_started) * 1000.0,
                          max_tokens=768,
                      )
                  if event["type"] == "delta":
                      _catchup_text += event["content"]
                      for ev in think_filter.feed(event["content"]):
                          if ev.get("event") == "delta":
                              # Summary JSON is subjective state, not an action
                              # channel: hide it BEFORE interpreting body/INNER tags.
                              clean = narration_filter.feed(
                                  tag_filter.feed(tool_markup_filter.feed(memory_filter.feed(dstate_filter.feed(ev["content"]))))
                              )
                              clean = _INLINE_TOOL_MARKER_RE.sub("", clean)
                              async for action_event in _drain_body_actions():
                                  yield action_event
                              if clean and _control_failure_pending and not _control_feedback_sent:
                                  clean = ""
                              if clean and _quiet_life_turn:
                                  # Quiet life round stays silent here too.
                                  clean = ""
                              if clean:
                                  yield _sse({"event": "delta", "content": clean})
                          else:
                              yield _sse(ev)
                  elif event["type"] == "think":
                      _think_chunk_buf.append(event.get("content", ""))
                      _flush_think()
                      yield _sse({"event": "think", "content": event["content"]})
                  elif event["type"] == "tool_call":
                      # tools=None: a stray call means the model ignored the
                      # instruction. Never execute it — that would restart
                      # the very loop this catch-up exists to end.
                      log.warning(
                          "ignoring tool call during forced final answer: %s",
                          event.get("name", "?"),
                      )
                  elif event["type"] == "error":
                      raise RuntimeError(event.get("message") or "provider error")
                  elif event["type"] == "end":
                      _flush_think(force=True)
                      break
          except Exception as exc:
              log.warning("forced final answer failed: %s", exc)
              _emit("error", "错误", str(exc))
              yield _sse({"event": "error", "message": str(exc)})
              _release_active_provider()
              return

          if _catchup_text.strip():
              full_text += _catchup_text
              _vitals_completion_tokens += _estimate_tokens(_catchup_text)
          if not _visible_reply_text(_catchup_text):
              if _is_proactive:
                  # A quiet proactive/continuous round is allowed to end with
                  # no visible text. DSTATE / next_chat processing still runs.
                  log.info("proactive round ended silently")
              else:
                  log.warning("forced final answer was empty; emitting visible fallback")
                  yield _sse({"event": "delta", "content": TOOL_LOOP_FALLBACK_TEXT})
                  full_text += TOOL_LOOP_FALLBACK_TEXT
          # A silent [INNER:…] produced during the catch-up must not tell the
          # renderer to hide the bubble that now carries a real answer. On a
          # quiet life round there is no answer to protect: the inner event is
          # the only signal that lets the renderer hide the bubble, so keep it.
          if not _quiet_life_turn:
              _inner_queue.clear()

      # The tool loop is over: no builtin handler can run for this turn
      # anymore, so release the provider reference now (also covers the
      # normal-completion path, not just the error returns above).
      _release_active_provider()

    # ── 生命体征 finally（点 2/6/7 落账）──────────────────────────────
    # Runs on EVERY exit of the billed region: normal end, provider error,
    # client disconnect (GeneratorExit). Every round that REACHED the
    # provider (user / proactive / silent narration / error / quiet) drains
    # exactly once and ticks the ledger exactly once — except a round
    # short-circuited BEFORE the provider (D-quiet, asleep / forced-off /
    # died proactive): no work = no round. Fast entries belong to the
    # future reflex layer (drain_small call site), not here.
    finally:
        _v = get_vitals()
        _lg = get_ledger()
        _tokens = int(_vitals_prompt_tokens)
        _reached_provider = not _vitals_short_circuited
        # completion tokens: per-iteration accumulation is folded into
        # full_text below via the accumulated buffer + final text.
        try:
            _all_text = " ".join(
                [p for p in (accumulated_full_text or []) if p] + ([full_text] if (full_text or "").strip() else [])
            )
            _completion = _estimate_tokens(_all_text)
        except Exception:
            _completion = 0
        _completion = max(_completion, _vitals_completion_tokens)
        if _reached_provider and _v is not None:
            try:
                _v.drain_main(
                    tokens=_tokens + _completion,
                    thinking=bool(req.thinking),
                    temperature=float(_round_temperature),
                )
                telemetry.inc("vitals.drain_main.rounds")
            except Exception as exc:
                _note_failure("vitals_drain_main", exc)
        _tick_result = None
        if _reached_provider and _lg is not None:
            try:
                _tick_result = _lg.round_tick()
            except Exception as exc:
                _note_failure("ledger_round_tick", exc)
        if _tick_result is not None:
            # Publish the NEXT round's pending state (organize mark / overflow
            # latch); the next round's start reads this slot. A real user
            # turn never consumed the organize prompt (it must not be
            # hijacked), so its slot is still pending — the tick's "no mark
            # this time" must not overwrite it. (A proactive round consumed
            # and EXECUTED the organize, so its normal publish stands.)
            try:
                if (
                    _user_turn_text is not None
                    and _pending_organize_kind
                    and not _tick_result.get("organize_due")
                ):
                    _tick_result = dict(_tick_result)
                    _tick_result["organize_due"] = True
                    _tick_result["organize_kind"] = _pending_organize_kind
                _pending_vitals_round["organize_kind"] = (
                    str(_tick_result.get("organize_kind"))
                    if _tick_result.get("organize_due") and _tick_result.get("organize_kind")
                    else None
                )
                _pending_vitals_round["overflow"] = bool(_tick_result.get("overflow"))
            except Exception:
                pass
        # Ledger slow entry: every main-brain round records its trimmed
        # visible reply (quiet rounds use the inner voice / a stub).
        if _reached_provider and _lg is not None:
            try:
                _ledger_text = _visible_reply_text(_all_text)
                if not _ledger_text.strip():
                    _inner_text = "\n".join(_round_inner_parts).strip()
                    _ledger_text = _inner_text or "安静的一轮"
                _lg.record_slow(_ledger_text.strip()[:2000])
            except Exception as exc:
                _note_failure("ledger_record_slow", exc)
        if _reached_provider:
            try:
                telemetry.inc("vitals.rounds_billed")
            except Exception:
                pass

    # ── DSTATE：更新 D 的滚动总结，并从所有下游文本里剥掉隐藏块 ──
    # The streamed block was already hidden by DStateFilter; the raw text
    # is kept only long enough to update/save the per-theme store, then
    # every visible/persisted copy below uses the cleaned text. The
    # summary itself lives ONLY in the D store (never in memory/history).
    _d_summary_system = False
    try:
        _d_updated = False
        if _cortex_store is not None:
            _d_updated, full_text = _cortex.update_from_round_text(
                _cortex_store, full_text
            )
            if _d_updated:
                log.info(
                    "cortex: DSTATE applied (summary_chars=%d)",
                    len(_cortex_store.summary),
                )
            else:
                actual_text = _visible_reply_text(" ".join([*accumulated_full_text, full_text]))
                notes = ["【系统记录：本轮未提供模型主观总结】"]
                for receipt in reversed(_round_receipts[-8:]):
                    notes.append(f"执行回执：{receipt['name']}={receipt['status']}；{receipt['summary']}")
                public_text = "" if _quiet_life_turn else actual_text
                inner_text = "\n".join(_round_inner_parts)
                if _quiet_life_turn and actual_text:
                    inner_text = (inner_text + "\n" + actual_text).strip()
                if public_text:
                    notes.append("我实际说过：" + json.dumps(public_text[:300], ensure_ascii=False))
                if inner_text:
                    notes.append("我的心声记录：" + json.dumps(inner_text[:300], ensure_ascii=False))
                if len(notes) > 1:
                    _cortex_store.update({"summary": "\n".join(notes)})
                    _cortex_store.save()
                    _d_updated = True
                    _d_summary_system = True
        else:
            full_text = _cortex.strip_dstate_blocks(full_text)
    except Exception as exc:
        _note_failure("cortex_dstate_update", exc)
        full_text = _cortex.strip_dstate_blocks(full_text)
    if _cortex_store is not None:
        _d_state_text = str(getattr(_cortex_store, "summary", "") or "")
        _emit(
            "dstate",
            "DSTATE 更新",
            _d_state_text,
            updated=bool(_d_updated),
            source="system" if _d_summary_system else "model",
            chars=len(_d_state_text),
        )
    # Earlier tool rounds must never carry a hidden block downstream either
    # (MEM extraction, NEXT_CHAT, chat history, final answer).
    if accumulated_full_text:
        accumulated_full_text = [
            _cortex.strip_dstate_blocks(part) for part in accumulated_full_text
        ]

    # ── Quiet life round: the surface was silent, the mind was not ──
    # D chose "quiet" on a proactive round, so no visible delta was yielded.
    # Anything the model nevertheless wrote as visible body is folded into
    # the hidden inner-voice store, so a model that forgot [INNER:] still
    # leaves a thought. Pure [INNER:…] content was already stored by
    # _on_inner, and _visible_reply_text() strips it, so it is never
    # double-stored here.
    if _quiet_life_turn:
        _hidden = _visible_reply_text("\n".join([*accumulated_full_text, full_text]))
        if _hidden:
            _round_inner_parts.append(_hidden)
            try:
                if _ephemeral_ref is not None:
                    _ephemeral_ref.add_thought(
                        "（心声）" + _hidden, source="quiet_life"
                    )
            except Exception as exc:
                _note_failure("quiet_life_thought", exc)
        log.info("quiet life round: inner voice hidden (%d chars)", len(_hidden))
        try:
            telemetry.inc("decision.quiet.life")
        except Exception:
            pass

    # ── 记忆整理轮落账（点 7）：审查/总结结果写 organize，永不删除 ──
    # recap 额外 set_recap：正文保存在台账里，下一条真实用户消息附上。
    # 组织轮的正文 = 用户可见回复（安静轮退回心声）。只在主动轮消费：
    # 真实用户轮不落 organize 账、不清槽位（未被劫持的轮没有整理产物，
    # 槽位等下一个主动轮）。
    if _pending_organize_kind and _user_turn_text is None:
        _lg7 = get_ledger()
        if _lg7 is not None:
            try:
                _organize_visible = _visible_reply_text(
                    " ".join([*accumulated_full_text, full_text])
                ).strip()
                if not _organize_visible:
                    _organize_visible = (
                        "\n".join(_round_inner_parts).strip() or "（这轮没有产出）"
                    )
                _lg7.record_organize(_organize_visible)
                if _pending_organize_kind == "recap":
                    _lg7.set_recap(_organize_visible)
                log.info(
                    "ledger: organize round recorded (kind=%s chars=%d)",
                    _pending_organize_kind, len(_organize_visible),
                )
            except Exception as exc:
                _note_failure("ledger_record_organize", exc)
        _pending_organize_kind = None  # cleared after the round completes

    # ── Brain stream: did anything visible reach the surface? ─────────
    _brain_parts = list(accumulated_full_text)
    if full_text.strip():
        _brain_parts.append(full_text)
    _brain_visible = _visible_reply_text(" ".join(_brain_parts))
    if _quiet_life_turn or (_is_proactive and not _brain_visible):
        _hidden_chars = _inner_chars
        if _quiet_life_turn:
            _hidden_chars += len(_brain_visible)
        _emit(
            "quiet",
            "安静 · 心声藏起来",
            hidden_chars=_hidden_chars,
            reason="quiet_decision" if _quiet_life_turn else "proactive_no_text",
        )
    else:
        _emit("speak", "说出来", _brain_visible, chars=len(_brain_visible))

    # ── Death note: gateway-side memory extraction (死亡遗嘱) ────────
    # Placed before ANY further yield: if the client died mid-stream the
    # generator gets GeneratorExit at the next yield, so this is the last
    # guaranteed chance to persist memories. The renderer's later POST
    # /api/events/extract is an idempotent no-op for the same text.
    _parts = list(accumulated_full_text)
    if full_text.strip():
        _parts.append(full_text)
    _combined = " ".join(_parts)

    if _extraction_hook is not None:
        try:
            if _combined.strip():
                _extraction_hook(_combined)
        except Exception as exc:
            log.warning("gateway-side extraction failed: %s", exc)

    # Missing MEM block: ask the same provider for JSON only, off the SSE
    # critical path. The primary hook above has already preserved any block
    # that did exist; run_fallback independently refuses short/proactive turns.
    if (
        _extraction_hook is not None
        and _should_log_conversation(req, conversation_dir)
        and os.environ.get("PET_EXTRACT_FALLBACK", "1") != "0"
    ):
        try:
            task = asyncio.create_task(_extract_fallback.run_fallback(
                provider,
                _user_text_for_log,
                _combined,
                _extraction_hook,
            ))
            _fallback_tasks.add(task)
            task.add_done_callback(_fallback_task_done)
        except Exception as exc:
            _note_failure("memory_extraction_fallback_schedule", exc)

    # ── 对话日志：睡眠压缩的原始输入（2026-10-19）──
    # Independent of extraction: a failing MEM hook must not drop the raw
    # day. Only real user turns are logged (see _should_log_conversation) —
    # narrator/debug requests share this stream and would poison the log.
    try:
        if _should_log_conversation(req, conversation_dir):
            from .memory.conversation_log import append_turn as _append_turn
            _reply_log = " ".join(p.strip() for p in _parts if p.strip()).strip()
            if _user_text_for_log:
                _append_turn("user", _user_text_for_log, memory_dir=conversation_dir)
            if _reply_log:
                _append_turn("assistant", _reply_log, memory_dir=conversation_dir)
    except Exception as _log_exc:
        _note_failure("conversation_log", _log_exc)

    _tail_from_think = ""
    for ev in think_filter.flush():
        if ev.get("event") == "delta":
            _tail_from_think += tag_filter.feed(tool_markup_filter.feed(memory_filter.feed(dstate_filter.feed(ev["content"]))))
            async for action_event in _drain_body_actions():
                yield action_event
        else:
            yield _sse(ev)
    # 心声 tail: a dangling [INNER:… salvaged at flush (unclosed tag) fires
    # on_inner AFTER the loop above — drain it here so the renderer still
    # learns the turn was silent.
    _tail_from_think += tag_filter.feed(tool_markup_filter.feed(memory_filter.feed(dstate_filter.flush()) + memory_filter.flush()) + tool_markup_filter.flush()) + tag_filter.flush()
    async for action_event in _drain_body_actions():
        yield action_event
    while _inner_queue:
        yield _sse(_inner_queue.pop(0))
    # The narration filter holds the last (newline-less) line of the reply
    # in its buffer — flush it unconditionally, think_filter.flush() may
    # legitimately have nothing left to say.
    _clean_tail = narration_filter.feed(_tail_from_think) + narration_filter.flush()
    # The DSTATE tail was already dropped before body-tag interpretation.
    if _clean_tail and not _quiet_life_turn:
        yield _sse({"event": "delta", "content": _clean_tail})

    # ── Emotion tag extraction ──
    # The LLM is instructed (via system prompt) to end replies with
    # [EMOTION:happy] etc. Parse it from the accumulated text across all
    # tool-calling iterations and push the emotion to the pet via the
    # existing ClawdBridge state path. The renderer strips the tag from
    # the visible text separately.
    # Use the LAST emotion tag in the reply — small models sometimes emit
    # an intermediate tag (e.g. [EMOTION:neutral]) before settling on a
    # final one. Also tolerate whitespace after the colon. The renderer
    # sends the emotion/next_chat instruction to ALL providers (remote
    # and local) via body.system, so we parse for every provider.
    if full_text.strip():
        accumulated_full_text.append(full_text)
    combined_text = " ".join(accumulated_full_text) if accumulated_full_text else full_text

    _emotion_matches = _EMOTION_RE.findall(combined_text)
    _emotion = _emotion_matches[-1].lower() if _emotion_matches else "neutral"
    log.info("emotion parsed: %s (all_matches=%s)", _emotion, _emotion_matches)

    # ── Proactive chat scheduling ──
    # Proactive chat: model outputs [NEXT_CHAT:N] where N is integer seconds.
    # 0 means "no proactive chat" — an explicit choice to stay silent,
    # always honoured as-is. Sent via SSE so renderer can setTimeout.
    _next_chat_re = re.compile(r"\[NEXT_CHAT:\s*(\d+)\s*(?:s|sec|seconds)?\s*\]", re.IGNORECASE)
    _nc_match = _next_chat_re.search(combined_text)
    if _nc_match:
        _nc_seconds = max(0, int(_nc_match.group(1)))
        # explicit=True: the model itself chose this gap.
        yield _sse({"event": "next_chat", "seconds": _nc_seconds, "explicit": True})
    elif not req.silent:
        # ── 说话冲动：潜意识兜底 ──
        # The model forgot to decide when to speak next. The subconscious
        # decides instead, from mood + how long it has been talking into
        # the void + how much the mind has been stirring. The model's own
        # explicit silence ([NEXT_CHAT:0]) never reaches this branch.
        # 说话冲动现在是可插拔器官：organ_impulse 通过内核提供 "impulse"
        # 服务（它的 config 暴露了基准/上下限/心情系数/退避倍率，模型和
        # 用户都能调）。器官被停用时自动降级回内置实现，聊天不会崩。
        _impulse_svc = _organ_service("impulse", _impulse_module)
        # 直觉压力：决策循环最近一条强心声（已按半衰期衰减，用户回应后
        # 为 0）。它只能让下一次开口稍微提前——动作与措辞仍由模型决定。
        # 读一次即 consume，防止同一条闪念反复缩短后续每个间隙。
        _loop = _decision_loop_ref
        _intuition_pressure = 0.0
        if _loop is not None:
            try:
                _intuition_pressure = _loop.pressure()
            except Exception as exc:
                _note_failure("intuition_pressure", exc)
        _intuition_pressure = max(0.0, min(1.0, float(_intuition_pressure or 0.0)))
        _impulse_kwargs = {
            "emotion_index": mood_store.emotion_index if mood_store is not None else 0.0,
            "proactive_streak": _proactive_streak,
            "recall_count": _organ_service("recall", _recall_module).get_session_recall_count(),
        }
        try:
            try:
                _impulse_gap = _impulse_svc.compute_gap(
                    **_impulse_kwargs, intuition_pressure=_intuition_pressure,
                )
            except TypeError as exc:
                # organ_impulse lives in the user's plugins dir and is never
                # overwritten, so an old seed may predate the pressure
                # argument. Degrade to the legacy gap instead of breaking
                # every "forgot [NEXT_CHAT]" turn.
                _note_failure("impulse_pressure_unsupported", exc)
                _impulse_gap = _impulse_svc.compute_gap(**_impulse_kwargs)
        finally:
            # The hunch was spent on this gap, whether or not the organ
            # accepted it. One button, one press.
            if _loop is not None:
                try:
                    _loop.consume_pressure()
                except Exception as exc:
                    _note_failure("intuition_pressure_consume", exc)
        log.info(
            "speech impulse: no [NEXT_CHAT] tag, subconscious gap=%ds "
            "(streak=%d, pressure=%.2f)",
            _impulse_gap, _proactive_streak, _intuition_pressure,
        )
        # explicit=False: subconscious default, not the model's own choice.
        yield _sse({"event": "next_chat", "seconds": _impulse_gap, "explicit": False})

    if not req.silent:
        bridge.post("attention", emotion=_emotion)
        # Emotion index: the model's chosen tag moves the index (calm=0,
        # happy +0.1, sad -0.1, …), which modulates the NEXT reply's
        # temperature. Silent requests (narrator / classifier) never move it.
        if mood_store is not None:
            mood_store.apply_emotion_tag(_emotion)
    if _continuity_store is not None and not req.silent:
        try:
            _continuity_store.record_round(
                _round_id, _round_started_at, time.time(), _is_proactive,
                "" if _quiet_life_turn else _brain_visible,
                "\n".join(_round_inner_parts), list(_round_receipts),
            )
            _continuity_store.save()
        except Exception as exc:
            _note_failure("continuity_record_round", exc)
    if not req.silent:
        _emit(
            "receipt", "系统 · 本轮事实回执",
            ("安静" if _quiet_life_turn or not _brain_visible else "已发言")
            + f"；执行回执 {len(_round_receipts)} 条",
            receipts=list(_round_receipts),
        )
    telemetry.observe("chat.turn_seconds", time.perf_counter() - turn_started)
    _emit(
        "round_end",
        "本轮结束",
        elapsed_ms=(time.perf_counter() - turn_started) * 1000.0,
        iterations=iteration,
        tools=_tool_call_count,
        provider=provider.name,
    )
    if _reflex_decision is not None:
        # Escalated reflex round: pace the NEXT tick (renderer honors the
        # event in its own phase; unknown SSE events are ignored safely
        # today).
        yield _sse({
            "event": "next_check_s",
            "seconds": int(_reflex_decision.get("next_check_s") or 0),
        })
    yield _sse({"event": "end"})


async def _blocking_chat_provider(
    registry: ProviderRegistry,
    mcp_manager: MCPManager,
    bridge: ClawdBridge,
    req: ChatRequest,
    server: LlamaServer,
    server_state: dict,
    mood_store: Optional[MoodStore] = None,
    lora_arr: Optional[list[dict]] = None,
    conversation_dir: Optional[Path] = None,
) -> dict:
    """Non-streaming provider-based chat. Accumulates the stream into a response."""
    collector: list[str] = []
    async for sse_bytes in _stream_chat_provider(
        registry, mcp_manager, bridge, req, server, server_state, mood_store, lora_arr,
        conversation_dir,
    ):
        # Parse SSE to collect content
        line = sse_bytes.decode("utf-8", "ignore")
        if line.startswith("data: "):
            import json as _json
            try:
                obj = _json.loads(line[6:].strip())
                if obj.get("event") == "delta":
                    collector.append(obj.get("content", ""))
            except _json.JSONDecodeError:
                pass
    return {"content": "".join(collector), "thinking": None}
