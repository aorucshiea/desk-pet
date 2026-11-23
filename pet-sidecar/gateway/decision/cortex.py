"""cortex.py - the D (decision/intuition) core for the continuous life loop.

The main LLM remains the author of the rolling life summary and weighted
options.  D only turns that state into three fast choices (thinking, reaction,
action), asking Laya all three questions in one ``/v1/systemone`` request.
There is deliberately no second LLM call and no timer loop here.

Passive and autonomous rounds use :func:`passive_decision`; the LLM-facing
active tool uses :func:`active_decision`.  Both share the same pipeline and
use explicit safe defaults whenever Laya is unavailable or malformed.
Caller deadlines do not free the per-loop gate while HTTP is still in flight.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import random
import re
import secrets
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import httpx

from .laya_router import LAYA_MODEL, _endpoint_url, _laya_enabled
from .sampler import (
    _append_record,
    _now_iso,
    _posterior,
    _resolve_timeout,
)

logger = logging.getLogger(__name__)

DEFAULT_ACTION_OPTIONS = [
    {"id": "speak", "label": "说", "weight": 0.35},
    {"id": "walk", "label": "走", "weight": 0.15},
    {"id": "look", "label": "看", "weight": 0.20},
    {"id": "quiet", "label": "安静", "weight": 0.20},
    {"id": "think", "label": "想", "weight": 0.15},
    {"id": "other", "label": "其他", "weight": 0.10},
]
DEFAULT_STRENGTH_OPTIONS = [
    {"id": "no_thinking", "label": "不思考", "weight": 0.8},
    {"id": "low", "label": "low", "weight": 0.2},
]
DEFAULT_REACTION_OPTIONS = [
    {"id": "small", "label": "小", "weight": 0.25},
    {"id": "medium", "label": "中", "weight": 0.50},
    {"id": "large", "label": "大", "weight": 0.25},
]
REACTION_MAX_TOKENS = {"small": 2048, "medium": 3072, "large": 4096}

SUMMARY_MAX_TOKENS = 1024
D_SUMMARY_MAX_TOKENS = 64
D_CONTEXT_MAX_TOKENS = 64
LAYA_HTTP_DEADLINE = 30.0
# No asyncio primitive is shared across loops. Pending tasks keep their own
# clients alive; completion removes the loop key and consumes any exception.
_INFLIGHT: "Dict[asyncio.AbstractEventLoop, asyncio.Task]" = {}
_INFLIGHT_LOCK = threading.Lock()
_DSTATE_START = "<<<DSTATE>>>"
_DSTATE_END = "<<<DSTATEEND>>>"
_DSTATE_RE = re.compile(
    re.escape(_DSTATE_START) + r"(.*?)" + re.escape(_DSTATE_END),
    re.DOTALL | re.IGNORECASE,
)
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_RECENT_MAXSIZE = 64
_RECENT: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_RECENT_LOCK = threading.Lock()

_QUESTION_SPECS = {
    "thinking": "凭底层直觉选择这一轮的思考强度。",
    "reaction": "凭底层直觉选择这一轮持续反应的篇幅。",
    "action": "凭底层直觉选择接下来最自然的默认动作。",
}


def _copy_options(options: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    return [dict(option) for option in options]


_S_WEIGHTS = {"reflex", "light", "heavy"}
# A reflex answer is executed by the body before the main model runs; the
# only body action that can happen that way is a head turn.
REFLEX_DIRECTIONS = {"左": "left", "left": "left", "右": "right", "right": "right"}
S_ANSWERS_HEADER = "【你问直觉的问题（S）与它的回答】"


def _clean_questions(raw: Any) -> List[Dict[str, Any]]:
    """Validate the model's S questions: natural prompt + options + weight."""
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, Any]] = []
    for item in raw[:6]:
        if not isinstance(item, dict):
            continue
        q = str(item.get("q") or "").strip()[:120]
        options = item.get("options")
        if not q or not isinstance(options, list):
            continue
        opts = [str(o).strip()[:40] for o in options if str(o).strip()][:6]
        if len(opts) < 2:
            continue
        weight = str(item.get("weight") or "light").strip().lower()
        if weight not in _S_WEIGHTS:
            weight = "light"
        if weight == "reflex" and not all(o.lower() in REFLEX_DIRECTIONS for o in opts):
            weight = "light"
        out.append({"q": q, "options": opts, "weight": weight})
    return out


def strip_dstate_blocks(text: str) -> str:
    """Remove every complete DSTATE block and an unterminated trailing one."""
    if not isinstance(text, str):
        return text
    cleaned = _DSTATE_RE.sub("", text)
    # Any marker left after complete-block substitution starts an unfinished
    # trailing block.  Never expose its partial JSON to the user.
    marker = cleaned.lower().find(_DSTATE_START.lower())
    if marker >= 0:
        cleaned = cleaned[:marker]
    # A block split across tool rounds can leave only the closing marker;
    # like the MEMEND case, a marker is control data and never speech.
    cleaned = re.sub(re.escape(_DSTATE_END), "", cleaned, flags=re.IGNORECASE)
    return cleaned


def parse_dstate_block(text: str) -> Tuple[Optional[dict], str]:
    """Return the last complete JSON object and text safe for user display.

    Only the last complete block is considered; an earlier valid block does
    not mask a malformed latest block.  Complete and unfinished blocks are
    stripped even when JSON parsing fails.
    """
    if not isinstance(text, str):
        return None, text
    matches = list(_DSTATE_RE.finditer(text))
    cleaned = strip_dstate_blocks(text)
    if not matches:
        return None, cleaned
    try:
        value = json.loads(matches[-1].group(1).strip())
    except (json.JSONDecodeError, TypeError, ValueError):
        return None, cleaned
    if not isinstance(value, dict) or not isinstance(value.get("summary"), str):
        return None, cleaned
    return value, cleaned


def _is_cjk(char: str) -> bool:
    point = ord(char)
    return (
        0x3400 <= point <= 0x4DBF
        or 0x4E00 <= point <= 0x9FFF
        or 0xF900 <= point <= 0xFAFF
        or 0x20000 <= point <= 0x323AF
    )


def _token_spans(text: str):
    """Yield ``(end_index, estimated_tokens)`` without splitting words."""
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if _is_cjk(char):
            yield index + 1, 1.0
            index += 1
            continue
        match = _ASCII_WORD_RE.match(text, index)
        if match is not None:
            yield match.end(), 1.0
            index = match.end()
            continue
        # Python strings expose non-BMP characters as one code point on all
        # supported builds, so advancing one character cannot split a pair.
        yield index + 1, 0.25
        index += 1


def estimate_tokens(text: str) -> int:
    """Conservative deterministic estimate: CJK=1, ASCII word=1, other=.25.

    Whitespace is free.  The rounded-up result is an approximation for budget
    enforcement, not a claim about any particular model tokenizer.
    """
    if not text:
        return 0
    return int(math.ceil(sum(weight for _, weight in _token_spans(str(text)))))


def truncate_summary(text: str, max_tokens: int = SUMMARY_MAX_TOKENS) -> str:
    """Cut a summary to the estimated token budget on safe token boundaries."""
    if not isinstance(text, str) or not text or max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text
    used = 0.0
    end = 0
    for token_end, weight in _token_spans(text):
        if used + weight > max_tokens:
            break
        used += weight
        end = token_end
    # Defensive for unusual input constructed on a narrow Unicode build.
    if end and 0xD800 <= ord(text[end - 1]) <= 0xDBFF:
        end -= 1
    return text[:end]


def _validate_options(
    raw: Any,
    cap: int,
    *,
    allowed_ids: Optional[set] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Validate one complete section; ``None`` means keep the old section."""
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    cleaned: List[Dict[str, Any]] = []
    seen = set()
    for option in raw[:cap]:
        if not isinstance(option, dict):
            return None
        option_id = option.get("id")
        if (
            not isinstance(option_id, str)
            or not _ID_RE.fullmatch(option_id)
            or option_id in seen
            or (allowed_ids is not None and option_id not in allowed_ids)
        ):
            return None
        label = option.get("label")
        if not isinstance(label, str) or not label.strip():
            return None
        weight = option.get("weight", 1.0)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            return None
        value = float(weight)
        if not math.isfinite(value):
            return None
        seen.add(option_id)
        cleaned.append(
            {
                "id": option_id,
                "label": label.strip()[:80],
                "weight": max(0.0, min(1.0, value)),
            }
        )
    return cleaned or None


def _section(
    raw: Any,
    previous: Sequence[Mapping[str, Any]],
    cap: int,
    allowed_ids: Optional[set] = None,
) -> List[Dict[str, Any]]:
    validated = _validate_options(raw, cap, allowed_ids=allowed_ids)
    return validated if validated is not None else _copy_options(previous)


def _epoch_value(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        epoch = float(value)
    except OverflowError:
        return None
    return epoch if math.isfinite(epoch) else None


def _copy_receipts(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw[-8:] if isinstance(item, dict)]


def _receipt_completed(receipt: dict) -> bool:
    if receipt.get("status") not in ("ok", "success", "completed"):
        return False
    if receipt.get("kind") == "tool" and "applied" not in receipt:
        return True
    return receipt.get("applied") is True


class DStateStore:
    """Latest rolling D state; pass a distinct JSON path for each pet theme."""

    def __init__(self, path=None):
        self.path = Path(path).expanduser() if path is not None else None
        self.summary = ""
        self.action_options = _copy_options(DEFAULT_ACTION_OPTIONS)
        self.strength_options = _copy_options(DEFAULT_STRENGTH_OPTIONS)
        self.reaction_options = _copy_options(DEFAULT_REACTION_OPTIONS)
        self.questions: List[Dict[str, Any]] = []
        self.updated_at = ""
        self.last_decision: Optional[dict] = None
        self.last_user: dict = {}
        self.last_round: dict = {}
        if self.path is not None:
            self.load()

    def note_user(self, text: str, round_id: str, at: float) -> None:
        self.last_user = {
            "text": text[:500],
            "round_id": round_id,
            "received_at": at,
            "responded_at": None,
        }

    def record_round(
        self,
        round_id: str,
        started_at: float,
        finished_at: float,
        proactive: bool,
        spoken: str,
        inner: str,
        receipts: list[dict],
    ) -> None:
        self.last_round = {
            "round_id": round_id,
            "started_at": started_at,
            "finished_at": finished_at,
            "proactive": proactive,
            "spoken": spoken[:500],
            "inner": inner[:500],
            "receipts": _copy_receipts(receipts),
        }
        if not proactive and spoken.strip() and self.last_user.get("round_id") == round_id:
            self.last_user["responded_at"] = finished_at

    def update(self, block: dict) -> None:
        if not isinstance(block, dict):
            return
        summary = block.get("summary")
        if isinstance(summary, str):
            self.summary = truncate_summary(summary)
        if "action_options" in block:
            self.action_options = _section(
                block.get("action_options"), self.action_options, 8
            )
        if "strength_options" in block:
            self.strength_options = _section(
                block.get("strength_options"),
                self.strength_options,
                2,
                {"no_thinking", "low"},
            )
        if "reaction_options" in block:
            self.reaction_options = _section(
                block.get("reaction_options"),
                self.reaction_options,
                3,
                {"small", "medium", "large"},
            )
        if "questions" in block:
            self.questions = _clean_questions(block.get("questions"))
        self.updated_at = _now_iso()

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "action_options": _copy_options(self.action_options),
            "strength_options": _copy_options(self.strength_options),
            "reaction_options": _copy_options(self.reaction_options),
            "questions": [dict(q) for q in self.questions],
            "updated_at": self.updated_at,
            "last_decision": self.last_decision,
            "last_user": dict(self.last_user),
            "last_round": (
                {
                    **self.last_round,
                    "receipts": _copy_receipts(self.last_round.get("receipts")),
                }
                if self.last_round else {}
            ),
        }

    def load(self) -> None:
        """Best-effort load; corrupt or partially invalid data keeps defaults."""
        if self.path is None:
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if not isinstance(data, dict):
                return
            summary = data.get("summary")
            if isinstance(summary, str):
                self.summary = truncate_summary(summary)
            if "action_options" in data:
                self.action_options = _section(
                    data.get("action_options"), self.action_options, 8
                )
            if "strength_options" in data:
                self.strength_options = _section(
                    data.get("strength_options"),
                    self.strength_options,
                    2,
                    {"no_thinking", "low"},
                )
            if "reaction_options" in data:
                self.reaction_options = _section(
                    data.get("reaction_options"),
                    self.reaction_options,
                    3,
                    {"small", "medium", "large"},
                )
            if "questions" in data:
                self.questions = _clean_questions(data.get("questions"))
            if isinstance(data.get("updated_at"), str):
                self.updated_at = data["updated_at"]
            if isinstance(data.get("last_decision"), dict):
                self.last_decision = data["last_decision"]
            last_user = data.get("last_user")
            if isinstance(last_user, dict):
                received_at = _epoch_value(last_user.get("received_at"))
                if (
                    isinstance(last_user.get("text"), str)
                    and isinstance(last_user.get("round_id"), str)
                    and received_at is not None
                ):
                    self.last_user = {
                        "text": last_user["text"][:500],
                        "round_id": last_user["round_id"],
                        "received_at": received_at,
                        "responded_at": _epoch_value(last_user.get("responded_at")),
                    }
            last_round = data.get("last_round")
            if isinstance(last_round, dict):
                started_at = _epoch_value(last_round.get("started_at"))
                finished_at = _epoch_value(last_round.get("finished_at"))
                if (
                    isinstance(last_round.get("round_id"), str)
                    and isinstance(last_round.get("proactive"), bool)
                    and started_at is not None
                    and finished_at is not None
                ):
                    self.last_round = {
                        "round_id": last_round["round_id"],
                        "started_at": started_at,
                        "finished_at": finished_at,
                        "proactive": last_round["proactive"],
                        "spoken": (
                            last_round["spoken"][:500]
                            if isinstance(last_round.get("spoken"), str) else ""
                        ),
                        "inner": (
                            last_round["inner"][:500]
                            if isinstance(last_round.get("inner"), str) else ""
                        ),
                        "receipts": _copy_receipts(last_round.get("receipts")),
                    }
        except Exception:
            logger.debug("D state load failed: %s", self.path, exc_info=True)

    def save(self) -> None:
        """Atomically persist state without letting I/O break a decision."""
        if self.path is None:
            return
        temporary = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(
                f".{self.path.name}.{secrets.token_hex(4)}.tmp"
            )
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(
                    self.to_dict(), handle, ensure_ascii=False, separators=(",", ":")
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary), str(self.path))
            temporary = None
        except Exception:
            logger.debug("D state save failed: %s", self.path, exc_info=True)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass


def update_from_round_text(store: DStateStore, full_text: str) -> Tuple[bool, str]:
    """Apply the last complete DSTATE block and always return display-safe text."""
    block, cleaned = parse_dstate_block(full_text)
    if block is None:
        return False, cleaned
    try:
        store.update(block)
        store.save()
    except Exception:
        # A state/persistence bug must not make the completed main round fail.
        logger.debug("D state update failed", exc_info=True)
        return False, cleaned
    return True, cleaned


def _state_value(state: Mapping[str, Any], key: str, default: Any = "") -> Any:
    value = state.get(key, default)
    return default if value is None else value


def format_continuity(store: DStateStore, now: float, proactive: bool) -> str:
    lines = [
        "【实时经历（系统事实）】",
        "本轮没有新的用户消息；历史对话已发生，不要把旧话当刚收到的问题"
        if proactive else "本轮有新的用户输入。",
    ]
    if store.last_user:
        user = store.last_user
        answered = "已回答" if user["responded_at"] is not None else "尚未回答"
        lines.append(
            f"最近用户消息：{now - user['received_at']:.1f}秒前收到；{answered}；"
            f"原文：{json.dumps(user['text'], ensure_ascii=False)}"
        )
    else:
        lines.append("尚无用户消息记录，不能推断用户离开多久。")
    receipts = []
    if store.last_round:
        last = store.last_round
        kind = "主动轮" if last["proactive"] else "用户输入轮"
        lines.extend([
            f"上一轮：round_id={last['round_id']}；完成时间（epoch）={last['finished_at']}；"
            f"{now - last['finished_at']:.1f}秒前结束；{kind}。",
            "当时实际说出：" + (json.dumps(last["spoken"], ensure_ascii=False) if last["spoken"] else "（无可见发言）"),
            "当时真实心声记录：" + (json.dumps(last["inner"], ensure_ascii=False) if last["inner"] else "（无心声记录）"),
        ])
        receipts = last["receipts"]
    else:
        lines.append("尚无已结束轮次记录。")
    if receipts:
        for index, receipt in enumerate(receipts, 1):
            result = "未完成（不能当作已执行）"
            if _receipt_completed(receipt):
                result = "成功（工具已返回，不等于身体动作）" if receipt.get("kind") == "tool" else "成功（已完成）"
            summary = receipt.get("summary")
            summary = summary[:500] if isinstance(summary, str) else "（无回执摘要）"
            lines.append(
                f"回执{index}：{result}；status={json.dumps(receipt.get('status'), ensure_ascii=False)}；"
                f"applied={json.dumps(receipt.get('applied'), ensure_ascii=False)}；"
                f"summary={json.dumps(summary, ensure_ascii=False)}"
            )
    else:
        lines.append("上一轮没有收到动作/感知完成回执；动作叙述或summary不是执行证据")
    lines.append("summary/S 判断是主观直觉，不构成已看见、已移动等证据；不要在正文复述时间和轮次ID。")
    return "\n".join(lines)


def build_laya_state(store: DStateStore, current_state: Optional[dict]) -> str:
    """Project facts first and a short D-only summary; never shrink the store."""
    state = current_state if isinstance(current_state, dict) else {}
    summary = truncate_summary(store.summary, D_SUMMARY_MAX_TOKENS) or "（还没有生命总结）"
    user_text = str(_state_value(state, "user_text")).strip()[:80]
    proactive = bool(_state_value(state, "is_proactive", False))
    is_proactive = "true" if proactive else "false"
    now = _epoch_value(state.get("now"))
    if now is None:
        now = time.time()
    facts = [
        "【实时经历（系统事实）】",
        "本轮没有新的用户消息；旧话已发生。" if proactive else "本轮有新的用户输入。",
    ]
    if store.last_user:
        user = store.last_user
        answered = "已回答" if user["responded_at"] is not None else "尚未回答"
        facts.append(
            f"最近用户：{now - user['received_at']:.1f}秒前收到，{answered}；"
            + json.dumps(user["text"][:16], ensure_ascii=False)
        )
    else:
        facts.append("用户：无记录。")
    if store.last_round:
        last = store.last_round
        facts.append(
            f"上一轮{now - last['finished_at']:.1f}秒前结束；实际说出："
            + json.dumps(last["spoken"][:16], ensure_ascii=False)
            + "；心声：" + json.dumps(last["inner"][:16], ensure_ascii=False)
        )
    else:
        facts.append("上轮：无记录。")
    receipts = store.last_round.get("receipts", [])
    if receipts:
        completed = sum(_receipt_completed(receipt) for receipt in receipts)
        facts.append(f"完成回执：{completed}成功，{len(receipts) - completed}未完成。")
        for done, label in ((True, "成功回执"), (False, "未完成回执")):
            names = [
                str(receipt.get("name") or receipt.get("summary") or "未命名")[:40]
                for receipt in receipts if _receipt_completed(receipt) == done
            ]
            facts.append(label + "：" + ("、".join(names) or "无"))
    else:
        facts.append("无动作/感知完成回执。")
    facts.append("当前能力：无本轮观察回执；summary不是执行证据。")
    continuity = "\n".join(facts)
    mood_index = str(_state_value(state, "mood_index", 0))[:20]
    idle_sec = str(_state_value(state, "idle_sec", 0))[:20]
    last_action = str(_state_value(state, "last_action")).strip()[:40]
    provider_type = str(_state_value(state, "provider_type", "fast")).strip()[:20]
    fields = [
        f"is_proactive：{is_proactive}",
        f"mood_index：{mood_index}",
        f"idle_sec：{idle_sec}",
        f"provider_type：{provider_type}",
    ]
    if user_text:
        fields.insert(0, f"user_text：{user_text}")
    if last_action:
        fields.append(f"last_action：{last_action}（动作选择，非执行证据）")
    return f"{continuity}\n生命总结：\n{summary}\n当前状态：\n" + "\n".join(fields)


def _prior(options: Sequence[Mapping[str, Any]]) -> Dict[str, float]:
    total = sum(float(option["weight"]) for option in options)
    if total <= 0:
        share = 1.0 / len(options)
        return {str(option["id"]): share for option in options}
    return {
        str(option["id"]): float(option["weight"]) / total for option in options
    }


def _build_payload(
    state: str,
    option_sets: Mapping[str, Sequence[dict]],
    meta: Optional[Mapping[str, str]] = None,
) -> dict:
    meta = meta or {}
    questions = {}
    for qid, options in option_sets.items():
        questions[qid] = {
            "type": "choice",
            "instructions": meta.get(qid) or _QUESTION_SPECS.get(qid, "凭底层直觉选择。"),
            "criteria": {option["id"]: option["label"] for option in options},
        }
    return {"model": LAYA_MODEL, "state": state, "questions": questions}


def _parse_probabilities(raw: Any, ids: Sequence[str]) -> Optional[Dict[str, float]]:
    if not isinstance(raw, dict):
        return None
    values = {}
    for option_id in ids:
        value = raw.get(option_id, 0.0)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        if not math.isfinite(number):
            return None
        values[option_id] = max(0.0, number)
    total = sum(values.values())
    if total <= 0:
        return None
    return {option_id: value / total for option_id, value in values.items()}


def _parse_laya_answers(data: Any, option_sets: Mapping[str, Sequence[dict]]):
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        return None, "laya_invalid_payload"
    answers = data["answers"]
    parsed = {}
    for qid in option_sets:
        answer = answers.get(qid)
        ids = [option["id"] for option in option_sets[qid]]
        # S questions (s0…) are additive: if Laya omits one, skip it rather
        # than failing the whole decision. The fixed three must be present.
        if not isinstance(answer, dict):
            if qid.startswith("s"):
                continue
            return None, f"laya_invalid_payload:{qid}"
        raw_choice = answer.get("choice")
        choice = None
        if raw_choice is not None:
            if not isinstance(raw_choice, str):
                return None, f"laya_invalid_choice:{qid}"
            choice = raw_choice.strip() or None
            if choice is not None and choice not in ids:
                return None, f"laya_invalid_choice:{qid}:{choice[:24]}"
        probabilities = _parse_probabilities(answer.get("probabilities"), ids)
        if probabilities is not None:
            if choice is None:
                choice = max(ids, key=lambda option_id: probabilities[option_id])
            confidence = probabilities[choice]
        else:
            if choice is None:
                return None, f"laya_invalid_probabilities:{qid}"
            raw_confidence = answer.get("answer_confidence", 0.5)
            if isinstance(raw_confidence, bool) or not isinstance(
                raw_confidence, (int, float)
            ):
                return None, f"laya_invalid_confidence:{qid}"
            confidence = float(raw_confidence)
            if not math.isfinite(confidence):
                return None, f"laya_invalid_confidence:{qid}"
            confidence = max(0.0, min(1.0, confidence))
            rest = (1.0 - confidence) / (len(ids) - 1) if len(ids) > 1 else 0.0
            probabilities = {
                option_id: confidence if option_id == choice else rest
                for option_id in ids
            }
        parsed[qid] = {
            "choice": choice,
            "probabilities": probabilities,
            "confidence": confidence,
        }
    return parsed, ""


async def _laya_http(client, transport, url, payload, option_sets):
    """Own the whole HTTP lifetime, independent of the caller's short budget."""
    async def request(http):
        response = await http.post(
            url, json=payload, timeout=httpx.Timeout(LAYA_HTTP_DEADLINE)
        )
        try:
            if response.status_code != 200:
                return None, f"laya_http_{response.status_code}"
            try:
                data = response.json()
            except Exception as exc:
                return None, f"laya_invalid_json:{type(exc).__name__}"
            return _parse_laya_answers(data, option_sets)
        finally:
            await response.aclose()

    async def exchange():
        if client is not None:
            # Borrowed clients remain caller-owned and are never closed here.
            return await request(client)
        # Creating/closing inside the shielded task keeps this client alive
        # after passive/active_decision has already returned a timeout.
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(LAYA_HTTP_DEADLINE),
            transport=transport,
            trust_env=False,
        ) as http:
            return await request(http)

    try:
        return await asyncio.wait_for(exchange(), timeout=LAYA_HTTP_DEADLINE)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return None, "laya_timeout"
    except Exception as exc:
        return None, f"laya_error:{type(exc).__name__}"


def _http_finished(task: asyncio.Task) -> None:
    # Consume even exceptions from a task whose caller timed out/cancelled.
    try:
        task.exception()
    except asyncio.CancelledError:
        pass
    with _INFLIGHT_LOCK:
        loop = task.get_loop()
        if _INFLIGHT.get(loop) is task:
            del _INFLIGHT[loop]


async def _ask_laya(
    client: Optional[httpx.AsyncClient],
    payload: dict,
    timeout: float,
    option_sets: Mapping[str, Sequence[dict]],
    *,
    transport=None,
):
    loop = asyncio.get_running_loop()
    with _INFLIGHT_LOCK:
        pending = _INFLIGHT.get(loop)
        if pending is not None and not pending.done():
            return None, "laya_busy"
        task = loop.create_task(
            _laya_http(client, transport, _endpoint_url(), payload, option_sets)
        )
        _INFLIGHT[loop] = task
        task.add_done_callback(_http_finished)
    try:
        # The gate belongs to real HTTP completion, not to this waiter. Late
        # answers are discarded: the task never writes stores or decision LRUs.
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return None, "laya_timeout"
    except Exception as exc:
        return None, f"laya_error:{type(exc).__name__}"


def _remember(decision: Dict[str, Any]) -> None:
    with _RECENT_LOCK:
        _RECENT[decision["decision_id"]] = decision
        _RECENT.move_to_end(decision["decision_id"])
        while len(_RECENT) > _RECENT_MAXSIZE:
            _RECENT.popitem(last=False)


def clear_recent() -> None:
    """Clear decisions and finished gates only; never cancel another inference."""
    with _RECENT_LOCK:
        _RECENT.clear()
    with _INFLIGHT_LOCK:
        for loop, task in list(_INFLIGHT.items()):
            if task.done():
                del _INFLIGHT[loop]


def _log_decision(decision: Mapping[str, Any], summary: str) -> None:
    record = {
        "type": "cortex_decision",
        "decision_id": decision["decision_id"],
        "ts": _now_iso(),
        "questions": decision["questions"],
        "strength": decision["strength"],
        "reaction": decision["reaction"],
        "action": decision["action"],
        "source": decision["source"],
        "summary_sha256": hashlib.sha256(summary.encode("utf-8")).hexdigest(),
    }
    _append_record(record, None)


async def _decision(
    store: DStateStore,
    current_state: Optional[dict],
    *,
    action_options=None,
    context: str = "",
    client=None,
    transport=None,
    timeout=None,
    passive: bool,
) -> dict:
    strength_options = _section(
        store.strength_options, DEFAULT_STRENGTH_OPTIONS, 2, {"no_thinking", "low"}
    )
    reaction_options = _section(
        store.reaction_options,
        DEFAULT_REACTION_OPTIONS,
        3,
        {"small", "medium", "large"},
    )
    raw_actions = store.action_options if action_options is None else action_options
    validated_actions = _validate_options(raw_actions, 8)
    action_options_clean = validated_actions or _copy_options(DEFAULT_ACTION_OPTIONS)
    option_sets = {
        "thinking": strength_options,
        "reaction": reaction_options,
        "action": action_options_clean,
    }
    # S questions: the main model's natural-language prompts for D. Additive;
    # they never replace the fixed three that drive budget/tempo/action.
    s_meta: Dict[str, str] = {}
    # Snapshot S prompts with their options before awaiting: a rolling update
    # must never pair an old HTTP answer with a new round's question.
    s_questions = [{**q, "options": list(q["options"])} for q in store.questions]
    for i, q in enumerate(s_questions):
        qid = f"s{i}"
        n = len(q["options"])
        option_sets[qid] = [
            {"id": o, "label": o, "weight": 1.0 / n} for o in q["options"]
        ]
        s_meta[qid] = q["q"]

    state = build_laya_state(store, current_state)
    if context:
        state += "\ncontext：" + truncate_summary(str(context).strip(), D_CONTEXT_MAX_TOKENS)
    summary = truncate_summary(store.summary)
    answers = None
    failure = ""
    if passive and (current_state or {}).get("is_proactive") is False:
        # A real user turn always speaks; waiting on CPU intuition only adds
        # latency, so answer immediately with the safe defaults instead.
        failure = "user_turn_no_wait"
    elif not _laya_enabled():
        failure = "laya_disabled"
    else:
        budget = _resolve_timeout(timeout)
        payload = _build_payload(state, option_sets, s_meta)
        try:
            answers, failure = await _ask_laya(
                client, payload, budget, option_sets, transport=transport
            )
        except Exception as exc:
            logger.debug("D decision unexpected Laya error", exc_info=True)
            answers, failure = None, f"laya_error:{type(exc).__name__}"

    source = "laya+prior" if answers is not None else "prior"
    safe_choices = {"thinking": "no_thinking", "reaction": "small", "action": "quiet"}
    if answers is None:
        failure = (failure or "laya_unavailable") + "|safety_default"
        # Passive failures must be silent even when the model gave quiet zero
        # weight or omitted it. Active tools must retain legal custom actions.
        if not passive and not any(o["id"] == "quiet" for o in action_options_clean):
            safe_choices["action"] = action_options_clean[0]["id"]
            failure += ":legal_custom_action"
        defaults = {
            "thinking": DEFAULT_STRENGTH_OPTIONS,
            "reaction": DEFAULT_REACTION_OPTIONS,
            "action": DEFAULT_ACTION_OPTIONS,
        }
        # Do not modify the option sets still held by a shielded HTTP task.
        option_sets = {qid: list(options) for qid, options in option_sets.items()}
        for qid, choice in safe_choices.items():
            if not any(o["id"] == choice for o in option_sets[qid]):
                option_sets[qid].append(dict(next(o for o in defaults[qid] if o["id"] == choice)))
    question_results = {}
    chosen_posteriors = []
    for qid in ("thinking", "reaction", "action"):
        options = option_sets[qid]
        ids = [option["id"] for option in options]
        prior = _prior(options)
        answer = answers.get(qid) if answers is not None else None
        posterior = _posterior(prior, answer["probabilities"]) if answer else dict(prior)
        if answers is None:
            choice = safe_choices[qid]
            shortlist = [choice]
            chosen_posteriors.append(0.0)  # safety is not probabilistic evidence
        else:
            # Cortex keeps every positive-weight action reachable, including
            # quiet. The independent sampler tool still uses its own top-3.
            shortlist = sorted(
                (option_id for option_id in ids if posterior[option_id] > 0),
                key=lambda option_id: -posterior[option_id],
            )
            seed_material = state + "\n" + qid + "\n" + "|".join(ids)
            seed = int(hashlib.sha256(seed_material.encode("utf-8")).hexdigest()[:16], 16)
            rng = random.Random(seed)
            choice = rng.choices(
                shortlist, weights=[posterior[option_id] for option_id in shortlist], k=1
            )[0]
            chosen_posteriors.append(posterior[choice])
        question_results[qid] = {
            "choice": choice,
            "posterior": posterior,
            "prior": prior,
            "laya_choice": answer["choice"] if answer else None,
            "laya_confidence": answer["confidence"] if answer else 0.0,
            "shortlist": shortlist,
        }

    strength = question_results["thinking"]["choice"]
    reaction = question_results["reaction"]["choice"]
    action = question_results["action"]["choice"]
    action_label = next(
        option["label"] for option in option_sets["action"] if option["id"] == action
    )
    s_answers = []
    for i, q in enumerate(s_questions):
        ans = answers.get(f"s{i}") if answers is not None else None
        s_answers.append({
            "q": q["q"],
            "weight": q["weight"],
            "choice": ans["choice"] if ans else None,
            "confidence": ans["confidence"] if ans else 0.0,
        })
    decision = {
        "decision_id": "d_" + secrets.token_hex(8),
        "thinking": strength == "low",
        "reasoning_effort": "low" if strength == "low" else None,
        "strength": strength,
        "reaction": reaction,
        "max_tokens": REACTION_MAX_TOKENS[reaction],
        "action": action,
        "action_label": action_label,
        "confidence": sum(chosen_posteriors) / len(chosen_posteriors),
        "source": source,
        "questions": question_results,
        "s_answers": s_answers,
        "summary_used": bool(summary.strip()),
        "reason": (
            f"{failure or 'laya'}|thinking={strength},reaction={reaction},action={action}"
        ),
    }
    _remember(decision)
    _log_decision(decision, summary)
    if passive:
        try:
            store.last_decision = decision
            store.save()
        except Exception:
            logger.debug("D last-decision persistence failed", exc_info=True)
    return decision


async def passive_decision(
    store: DStateStore,
    current_state: Optional[dict] = None,
    *,
    client=None,
    transport=None,
    timeout=None,
) -> dict:
    """Make one passive/autonomous D decision and remember it in the store."""
    return await _decision(
        store,
        current_state,
        client=client,
        transport=transport,
        timeout=timeout,
        passive=True,
    )


async def active_decision(
    store: DStateStore,
    current_state: Optional[dict] = None,
    *,
    action_options=None,
    context: str = "",
    client=None,
    transport=None,
    timeout=None,
) -> dict:
    """Make the same decision with optional transient actions and context.

    Unlike the passive path, this never changes or saves ``store``.
    """
    return await _decision(
        store,
        current_state,
        action_options=action_options,
        context=context,
        client=client,
        transport=transport,
        timeout=timeout,
        passive=False,
    )


def decision_to_prompt(decision: dict) -> str:
    """Format only the decision intuition; never leak the D-only summary."""
    strength = decision.get("strength", "no_thinking")
    strength_label = "不思考" if strength == "no_thinking" else str(strength)
    reaction = decision.get("reaction", "medium")
    reaction_label = {"small": "小", "medium": "中", "large": "大"}.get(
        reaction, str(reaction)
    )
    max_tokens = decision.get("max_tokens", REACTION_MAX_TOKENS["medium"])
    action_label = decision.get("action_label") or decision.get("action") or "看"
    text = (
        "【这一轮你的底层直觉】\n"
        f"思考强度：{strength_label}（D 只能给不思考/low；需要更深时用 pet_enter_slow）\n"
        f"持续反应：{reaction_label}（max_tokens={max_tokens}）\n"
        f"默认动作：{action_label}（只是默认，可以改；用户直接说话必须回应）"
    )
    if decision.get("action") == "think":
        text += (
            "\n这一轮是心声模式：不要输出给用户看的可见正文。"
            "用 [INNER:...] 写下你此刻真实的观察、想法、情绪；"
            "它会进入你的隐藏心声，不会给用户看。不要空着。"
        )
    s_answers = decision.get("s_answers") or []
    if s_answers:
        lines = [S_ANSWERS_HEADER]
        for item in s_answers:
            weight = item.get("weight", "light")
            choice = item.get("choice")
            ans = choice if choice is not None else "（直觉没答）"
            if weight == "reflex":
                turned = item.get("applied")
                if turned in ("left", "right"):
                    side = "左" if turned == "left" else "右"
                    note = f"反射：身体已经转向{side}边"
                else:
                    note = "反射没执行：按习惯处理，你可以否决"
            elif weight == "heavy":
                note = "重要：你必须自己确认要不要遵循"
            else:
                note = "习惯：直觉定了，你可以否决"
            lines.append(f"- {item.get('q','')} → {ans}（{note}）")
        text += "\n" + "\n".join(lines)
    return text


__all__ = [
    "DEFAULT_ACTION_OPTIONS",
    "DEFAULT_STRENGTH_OPTIONS",
    "DEFAULT_REACTION_OPTIONS",
    "REACTION_MAX_TOKENS",
    "strip_dstate_blocks",
    "parse_dstate_block",
    "estimate_tokens",
    "truncate_summary",
    "DStateStore",
    "update_from_round_text",
    "format_continuity",
    "build_laya_state",
    "passive_decision",
    "active_decision",
    "decision_to_prompt",
    "clear_recent",
]
