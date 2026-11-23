"""Shared primitives for the self-evolution workbench.

Candidate records are plain JSON; nothing in here imports the gateway app,
so the workbench can run even when the live server is broken.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Candidate id shape: sortable UTC timestamp + random suffix. The suffix is
# what makes a same-second proposal (a model retrying) a different candidate.
_CANDIDATE_ID_RE = re.compile(r"^ev_[0-9]{8}_[0-9]{6}_[0-9a-f]{6}$")
_PROMOTION_ID_RE = re.compile(r"^pr_[0-9]{8}_[0-9]{6}_[0-9a-f]{6}$")

# Lifecycle states stored in candidate.json "status". The *verdict* of the
# last evaluation lives in "eval_status" (verified/failed/unverified/error):
# a candidate can be evaluated-and-failed and still be inspected or discarded.
STATUS_PROPOSED = "proposed"
STATUS_EVALUATED = "evaluated"
STATUS_PROMOTED = "promoted"
STATUS_ROLLED_BACK = "rolled_back"
STATUS_DISCARDED = "discarded"

VERDICT_VERIFIED = "verified"
VERDICT_FAILED = "failed"
VERDICT_UNVERIFIED = "unverified"
VERDICT_ERROR = "error"

# A core edit or plugin source larger than this is treated as a binary/build
# artifact. It also keeps a runaway model from making the workbench write a
# multi-megabyte candidate into a JSON record.
MAX_TEXT_BYTES = 512 * 1024


class EvolveError(RuntimeError):
    """Base for every expected failure of the evolution workbench."""


class NotAllowed(EvolveError):
    """The request is outside the M1 safety envelope (paths, policy, ...)."""


class NotFound(EvolveError):
    """A candidate / promotion / source file does not exist."""


class PromotionRefused(EvolveError):
    """A candidate cannot be promoted, or a promotion was rolled back."""


class WorkbenchError(EvolveError):
    """Git / worktree / edit-application infrastructure failed."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return utc_now().isoformat()


def new_candidate_id() -> str:
    return f"ev_{utc_now():%Y%m%d_%H%M%S}_{secrets.token_hex(3)}"


def new_promotion_id() -> str:
    return f"pr_{utc_now():%Y%m%d_%H%M%S}_{secrets.token_hex(3)}"


def is_candidate_id(value: object) -> bool:
    return isinstance(value, str) and bool(_CANDIDATE_ID_RE.fullmatch(value))


def is_promotion_id(value: object) -> bool:
    return isinstance(value, str) and bool(_PROMOTION_ID_RE.fullmatch(value))


def sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def normalize_newlines(text: str) -> str:
    """Normalize CRLF/CR to LF.

    Windows checkouts are CRLF while the model sees numbered source lines;
    comparing normalized text is what makes an ``old`` snippet copied from
    ``read_source`` match the file it came from. The file's original line
    ending is restored on write.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write a file via a same-directory temp file + os.replace.

    A crash mid-write must never leave a truncated candidate.json or a
    half-written live plugin.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".evolve-", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_write_json(path: Path, payload: Any) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    atomic_write_text(path, body)


def read_json(path: Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def tail_lines(text: str, limit: int = 80) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-limit:])


def text_size_ok(text: str) -> bool:
    try:
        return len(text.encode("utf-8")) <= MAX_TEXT_BYTES
    except UnicodeEncodeError:
        return False
