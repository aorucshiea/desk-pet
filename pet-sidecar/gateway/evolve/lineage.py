"""Append-only lineage: the candidate's audit trail.

Every state change goes through one event shape so a reader can answer
"what did the pet change, when, based on which commit, and who approved it"
without reconstructing history from candidate files (which are mutable).
"""

from __future__ import annotations

from typing import Any

from .models import now_iso
from .store import EvolveStore


def append_event(
    store: EvolveStore,
    event: str,
    *,
    candidate: dict[str, Any] | None = None,
    candidate_id: str = "",
    kind: str = "",
    status: str = "",
    actor: str = "pet",
    **extra: Any,
) -> dict[str, Any]:
    record = candidate or {}
    cid = candidate_id or str(record.get("id", ""))
    item: dict[str, Any] = {
        "ts": now_iso(),
        "event": event,
        "id": cid,
        "kind": kind or str(record.get("kind", "")),
        "status": status or str(record.get("status", "")),
        "diff_sha256": str(record.get("diff_sha256", "")),
        "actor": actor,
    }
    for key, value in extra.items():
        if value is not None:
            item[key] = value
    store.append_lineage(item)
    return item


def tail(store: EvolveStore, limit: int = 5) -> list[dict[str, Any]]:
    return store.lineage_tail(limit)
