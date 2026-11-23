"""Splits a model stream into reasoning (<think>...</think>) and content events.

Ported from the legacy PyTorch bridge. The DeskPet chat template wraps
reasoning in <think>...</think>; we hold any text that *might* be the start
of a tag until we know for sure, then emit either a `think` event (when
expose=True) or drop it (when expose=False). Plain text outside the tag is
always emitted as a `delta` event.

The Electron renderer (see clawd-on-desk/src/pet-chat.html) consumes
this exact event vocabulary.

Also hosts ControlTagFilter (B-1): strips [EMOTION:…] / [NEXT_CHAT:…]
control tags from the delta stream mid-flight so the typewriter renderer
never flashes them at the user. DStateFilter hides the main LLM's
<<<DSTATE>>>…<<<DSTATEEND>>> rolling-summary block the same way.
"""

from __future__ import annotations

import re
from typing import Callable, Iterable


class ThinkBlockFilter:
    OPEN_TAG = "<think>"
    CLOSE_TAG = "</think>"

    def __init__(self, *, expose: bool, start_inside: bool = False) -> None:
        self.expose = expose
        self._buf = ""
        self._mode = "inside" if start_inside else "outside"

    def feed(self, piece: str) -> list[dict]:
        self._buf += piece
        return list(self._drain())

    def flush(self) -> list[dict]:
        out = list(self._drain(final=True))
        if self._buf:
            ev = "think" if (self._mode == "inside" and self.expose) else "delta"
            if self._mode == "inside" and not self.expose:
                pass  # drop residual reasoning when caller doesn't want it
            else:
                out.append({"event": ev, "content": self._buf})
            self._buf = ""
        return out

    def _drain(self, *, final: bool = False) -> Iterable[dict]:
        while self._buf:
            if self._mode == "outside":
                idx = self._buf.find(self.OPEN_TAG)
                if idx < 0:
                    safe_len = self._safe_emit_len(self._buf, self.OPEN_TAG)
                    if safe_len <= 0:
                        if final and self._buf:
                            yield {"event": "delta", "content": self._buf}
                            self._buf = ""
                        return
                    out, self._buf = self._buf[:safe_len], self._buf[safe_len:]
                    if out:
                        yield {"event": "delta", "content": out}
                    return
                if idx > 0:
                    out, self._buf = self._buf[:idx], self._buf[idx:]
                    yield {"event": "delta", "content": out}
                self._buf = self._buf[len(self.OPEN_TAG):]
                self._mode = "inside"
            else:  # inside <think>
                idx = self._buf.find(self.CLOSE_TAG)
                if idx < 0:
                    safe_len = self._safe_emit_len(self._buf, self.CLOSE_TAG)
                    if safe_len <= 0:
                        return
                    out, self._buf = self._buf[:safe_len], self._buf[safe_len:]
                    if out and self.expose:
                        yield {"event": "think", "content": out}
                    return
                head, self._buf = self._buf[:idx], self._buf[idx + len(self.CLOSE_TAG):]
                if head and self.expose:
                    yield {"event": "think", "content": head}
                self._mode = "outside"
                # Eat exactly one trailing "\n\n" the template inserts after </think>
                if self._buf.startswith("\n\n"):
                    self._buf = self._buf[2:]
                elif self._buf.startswith("\n"):
                    self._buf = self._buf[1:]

    @staticmethod
    def _safe_emit_len(buf: str, tag: str) -> int:
        """How many chars of `buf` can be emitted without crossing a partial tag tail."""
        max_keep = len(tag) - 1
        for keep in range(min(max_keep, len(buf)), 0, -1):
            if tag.startswith(buf[-keep:]):
                return len(buf) - keep
        return len(buf)


# ── B-1: control-tag filter for streaming deltas ─────────────────────────────
# [EMOTION:happy] / [NEXT_CHAT:300] are renderer control tags the model
# appends to its reply; [WALK:dx,dy] / [WALK_DESKTOP] are BODY commands
# (身体移动 — the model walks itself). Streaming them raw made them flash
# on screen until the renderer's final sanitize pass stripped them. This
# filter removes the tags from the stream itself, holding back any
# `[`-prefix tail that might still grow into a tag (max tag length is
# short, so the hold is tiny). WALK tags are additionally surfaced to the
# caller via on_control so the stream loop can emit body-command events.
#
# [INNER:…] is a different beast: 心声 — the model's own choice of output
# mode. When the model wraps its WHOLE reply in [INNER:…], the content is
# not for the user at all (自己在心里想): it must never leak a single
# delta to the renderer. Because the tag's inner text can span the entire
# reply across many deltas, the existing hold-back logic carries it (a
# growing "[INNER:…" prefix candidate is held; on ']' the complete tag is
# stripped wholesale). The captured content is surfaced via on_inner so
# the stream loop can write it to ephemeral memory (瞬忆) instead.

_CTRL_TAG_RE = re.compile(
    r"\[\s*(?:EMOTION|NEXT_CHAT|WALK_DESKTOP|WALK|TURN|INNER)\s*[^\]]*\]",
    re.IGNORECASE,
)
# The longest possible tag prefix we must hold before deciding it is not a
# tag: "[WALK_DESKTOP" → 13 chars. Holding 16 covers whitespace variants.
_CTRL_MAX_HOLD = 16
_CTRL_PREFIXES = ("[EMOTION:", "[NEXT_CHAT:", "[WALK:", "[WALK_DESKTOP", "[TURN:", "[INNER:")
_WALK_RE = re.compile(r"\[\s*WALK\s*:\s*(-?\d+)\s*,\s*(-?\d+)\s*\]", re.IGNORECASE)
_TURN_RE = re.compile(r"\[\s*TURN\s*:\s*(left|right)\s*\]", re.IGNORECASE)
_INNER_RE = re.compile(r"\[\s*INNER\s*:\s*([^\]]*)\]", re.IGNORECASE)
_DANGLING_INNER_RE = re.compile(r"^\s*\[\s*INNER\s*:\s*(.*)$", re.IGNORECASE | re.DOTALL)


class ControlTagFilter:
    """Scan controls in stream order, keeping nested INNER content opaque."""

    _NAMES = ("EMOTION", "NEXT_CHAT", "WALK", "WALK_DESKTOP", "TURN", "INNER")
    _DESKTOP_RE = re.compile(r"\[\s*WALK_DESKTOP\s*\]", re.IGNORECASE)

    def __init__(
        self,
        on_control: Callable[[str, int | None, int | None], None] | None = None,
        on_inner: Callable[[str], None] | None = None,
    ) -> None:
        self._on_control = on_control
        self._on_inner = on_inner
        self._reset()

    def _reset(self) -> None:
        self._mode = "text"
        self._parts: list[str] = []
        self._name = ""
        self._name_done = False
        self._depth = 0

    def feed(self, piece: str) -> str:
        """Consume only new characters; return text known not to be a control."""
        out: list[str] = []
        for char in piece:
            if self._mode == "inner":
                if char == "[":
                    self._depth += 1
                elif char == "]":
                    self._depth -= 1
                if self._depth == 0:
                    self._finish_inner()
                else:
                    # Append without copying or rescanning a growing INNER.
                    self._parts.append(char)
                continue

            if self._mode == "tag":
                self._parts.append(char)
                if char == "]":
                    self._finish_tag()
                continue

            if self._mode == "prefix":
                if char.isspace() and (not self._name or self._name in self._NAMES):
                    self._parts.append(char)
                    self._name_done = bool(self._name)
                    continue
                if self._name in self._NAMES and char in ":]":
                    self._parts.append(char)
                    if char == "]":
                        self._finish_tag()
                    elif self._name == "INNER":
                        self._mode = "inner"
                        self._depth = 1
                        self._parts.clear()
                    else:
                        self._mode = "tag"
                    continue
                name = self._name + char.upper()
                if not self._name_done and any(tag.startswith(name) for tag in self._NAMES):
                    self._name = name
                    self._parts.append(char)
                    continue
                # Not a control: release the prefix, then handle this character
                # normally (it may itself start a new '[' candidate).
                out.extend(self._parts)
                self._reset()

            if char == "[":
                self._mode = "prefix"
                self._parts.append(char)
            else:
                out.append(char)
        return "".join(out)

    def _finish_tag(self) -> None:
        tag = "".join(self._parts)
        name = self._name
        self._reset()
        if self._on_control is None:
            return
        if name == "WALK":
            match = _WALK_RE.fullmatch(tag)
            if match:
                self._on_control("walk", int(match.group(1)), int(match.group(2)))
        elif name == "TURN":
            match = _TURN_RE.fullmatch(tag)
            if match:
                self._on_control("turn:" + match.group(1).lower(), None, None)
        elif name == "WALK_DESKTOP" and self._DESKTOP_RE.fullmatch(tag):
            self._on_control("walk_desktop", None, None)

    def _finish_inner(self, *, final: bool = False) -> None:
        content = "".join(self._parts).strip()
        self._reset()
        if self._on_inner is not None and (content or not final):
            self._on_inner(content)

    def flush(self) -> str:
        """Salvage unfinished INNER; discard tails with a possible control name."""
        if self._mode == "inner":
            self._finish_inner(final=True)
            return ""
        # A lone '[' (with optional whitespace) still counts as plain text.
        out = "".join(self._parts) if self._mode == "prefix" and not self._name else ""
        self._reset()
        return out


# ── Tool-narration filter for streaming deltas ───────────────────────────────
# Tool-loop models narrate their own tool calls as markdown-italic asides
# ("_执行 recall..._", "_执行 pet_mood_now..._"). Those lines are pipeline
# chatter — the tool-call SSE events already tell the renderer what ran —
# so a line that is NOTHING BUT such a narration is dropped whole. Lines
# are only decided at a newline, so the filter buffers the tail until it
# can tell the line is complete.

# The status line's tool name may be the raw id (recall) OR a Chinese
# display label (记忆 / 调节奏…) — the gateway now shows Chinese labels in
# its own "_执行 X..._" asides. `\S+?` (non-greedy, no whitespace) covers
# both without matching whole sentences (those contain spaces).
_NARRATION_RE = re.compile(
    r"^\s*_{0,2}\s*执行\s+\S+?\s*(?:\.\.\.)?\s*_{0,2}\s*$"
)


class DStateFilter:
    """Hold back a ``<<<DSTATE>>>…<<<DSTATEEND>>>`` block while it streams.

    The main LLM ends every round with a hidden rolling-life-summary block
    for the D core. It must never flash on screen, so text from the start
    marker until the end marker is swallowed wholesale. An unterminated
    trailing block (and a partial start marker) is dropped at ``flush()``.
    """

    START_TAG = "<<<DSTATE>>>"
    END_TAG = "<<<DSTATEEND>>>"

    def __init__(self, start_tag: str = START_TAG, end_tag: str = END_TAG) -> None:
        self.START_TAG = start_tag
        self.END_TAG = end_tag
        self._buf = ""
        self._inside = False

    def feed(self, piece: str) -> str:
        self._buf += piece
        out: list[str] = []
        while self._buf:
            if self._inside:
                idx = self._buf.lower().find(self.END_TAG.lower())
                if idx < 0:
                    break  # still streaming the hidden JSON
                self._buf = self._buf[idx + len(self.END_TAG):]
                self._inside = False
                continue
            idx = self._buf.lower().find(self.START_TAG.lower())
            if idx < 0:
                safe = self._safe_emit_len(self._buf)
                if safe <= 0:
                    break
                out.append(self._buf[:safe])
                self._buf = self._buf[safe:]
                break
            if idx > 0:
                out.append(self._buf[:idx])
            self._buf = self._buf[idx + len(self.START_TAG):]
            self._inside = True
        return "".join(out)

    def flush(self) -> str:
        """Stream end: drop an unfinished block, emit the rest."""
        if self._inside:
            self._buf = ""
            self._inside = False
            return ""
        # A held partial start marker at the very end is most likely a
        # truncated block — never leak it. Anything else is plain text.
        out = self._buf
        if len(out) >= 3 and self.START_TAG.lower().startswith(out.lower()):
            out = ""
        self._buf = ""
        return out

    def _safe_emit_len(self, buf: str) -> int:
        max_keep = len(self.START_TAG) - 1
        for keep in range(min(max_keep, len(buf)), 0, -1):
            if self.START_TAG.lower().startswith(buf[-keep:].lower()):
                return len(buf) - keep
        return len(buf)


class ToolNarrationFilter:
    """Stateful line scrubber for self-narrated tool-call asides."""

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, piece: str) -> str:
        self._buf += piece
        if "\n" not in self._buf:
            return ""
        return self._drain(final=False)

    def flush(self) -> str:
        return self._drain(final=True)

    def _drain(self, *, final: bool) -> str:
        if not final and "\n" not in self._buf:
            return ""
        parts = self._buf.split("\n")
        self._buf = "" if final else parts.pop()
        kept = [p for p in parts if not _NARRATION_RE.match(p)]
        if not kept:
            return ""
        return "\n".join(kept) + ("\n" if not final else "")
