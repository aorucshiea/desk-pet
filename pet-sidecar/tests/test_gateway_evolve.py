"""Gateway wiring for the M1 self-evolution workbench.

The core engine is covered in test_evolve_*.py; these tests pin the pet's
tool surface: the tools exist only when the workbench booted, and their
handlers return text/JSON instead of raising into the tool loop.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from gateway import server as server_mod
from gateway.evolve.models import NotFound


class _FakeEvolve:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def inspect(self) -> str:
        return "REPO REPORT"

    def read_source(self, path: str, *, start: int = 1, end: int | None = None) -> str:
        self.calls.append(("read", path, start, end))
        return f"{start}\t{path}"

    def propose(self, **kwargs) -> dict:
        self.calls.append(("propose", kwargs["kind"]))
        return {
            "id": "ev_20261027_120000_abcdef",
            "kind": kwargs["kind"],
            "status": "proposed",
            "files_changed": ["pet-sidecar/gateway/foo.py"],
            "diff_sha256": "abc",
            "base_dirty": False,
        }

    def evaluate(self, candidate_id: str, *, full: bool = False) -> dict:
        self.calls.append(("evaluate", candidate_id, full))
        return {
            "candidate_id": candidate_id,
            "status": "verified",
            "counts": {"passed": 3, "failed": 0, "errors": 0, "skipped": 0},
            "new_failures": [],
        }

    def list_candidates(self, limit: int = 20) -> list[dict]:
        return [{"id": "ev_1", "status": "proposed"}][:limit]

    def show(self, candidate_id: str) -> dict:
        return {"id": candidate_id, "status": "evaluated"}

    def discard(self, candidate_id: str) -> dict:
        return {"id": candidate_id, "status": "discarded"}

    @property
    def store(self):
        raise AssertionError("not needed in these tests")


class _FakeMcp:
    async def list_all_tools(self):
        return []


class _Req:
    tools_enabled = True


@pytest.fixture
def fake_service(monkeypatch):
    svc = _FakeEvolve()
    monkeypatch.setitem(server_mod._evolve_service_ref, "svc", svc)
    yield svc
    server_mod._evolve_service_ref.pop("svc", None)


def _tool_names() -> set[str]:
    defs = asyncio.run(server_mod._gather_tools(_FakeMcp(), _Req()))
    return {d.name for d in (defs or []) if d.server_name == "pet-evolve"}


def test_evolve_tools_are_exposed_only_with_a_workbench(fake_service):
    assert {
        "pet_evolve_inspect",
        "pet_evolve_read",
        "pet_evolve_propose",
        "pet_evolve_evaluate",
        "pet_evolve_list",
        "pet_evolve_show",
        "pet_evolve_discard",
    } <= _tool_names()

    server_mod._evolve_service_ref.clear()
    assert _tool_names() == set()


def test_evolve_tool_read_passes_line_bounds(fake_service):
    out = asyncio.run(
        server_mod._evolve_tool(
            "pet_evolve_read",
            {"path": "pet-sidecar/gateway/server.py", "start": 10, "end": 20},
        )
    )
    assert out.startswith("10\t")
    assert fake_service.calls == [
        ("read", "pet-sidecar/gateway/server.py", 10, 20)
    ]


def test_evolve_tool_propose_returns_candidate_summary(fake_service):
    out = asyncio.run(
        server_mod._evolve_tool(
            "pet_evolve_propose",
            {
                "goal": "让日志更容易读",
                "plan": "改一行日志",
                "kind": "core",
                "edits": [{"path": "pet-sidecar/gateway/foo.py", "old": "a", "new": "b"}],
            },
        )
    )
    payload = json.loads(out)
    assert payload["candidate_id"].startswith("ev_")
    assert payload["kind"] == "core"
    assert "/" not in payload["candidate_id"]


def test_evolve_tool_requires_edits_and_candidate_id(fake_service):
    bad = asyncio.run(
        server_mod._evolve_tool(
            "pet_evolve_propose", {"goal": "g", "plan": "p", "edits": "nope"}
        )
    )
    assert "edits=" in bad
    missing = asyncio.run(server_mod._evolve_tool("pet_evolve_evaluate", {}))
    assert "candidate_id" in missing


def test_evolve_tool_returns_service_errors_as_text(monkeypatch, fake_service):
    def _missing(_cid):
        raise NotFound("candidate 不存在")

    monkeypatch.setattr(fake_service, "discard", _missing)
    out = asyncio.run(
        server_mod._evolve_tool("pet_evolve_discard", {"candidate_id": "ev_x"})
    )
    assert out == "[evolve] candidate 不存在"


def test_evolve_tool_gracefully_degrades_without_service():
    server_mod._evolve_service_ref.clear()
    out = asyncio.run(server_mod._evolve_tool("pet_evolve_inspect", {}))
    assert "没有启动" in out
