"""Service facade: propose/list/show/discard, lineage, inspection, errors."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.evolve.models import NotAllowed, NotFound
from gateway.evolve.service import EvolveService
from _evolve_testkit import (
    PASSING_EDIT,
    make_repo,
    make_service,
    requires_git,
    write,
)

pytestmark = requires_git


def _lineage(service) -> list[dict]:
    path = service.data_dir / "lineage.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_propose_list_show_discard_and_lineage(tmp_path):
    repo = make_repo(tmp_path)
    service = make_service(tmp_path, repo)
    record = service.propose(
        goal="add a multiplication helper", plan="append it", edits=[PASSING_EDIT], title="mul"
    )
    candidate_id = record["id"]

    assert (service.data_dir / "candidates" / candidate_id / "candidate.json").is_file()
    assert (service.data_dir / "candidates" / candidate_id / "diff.patch").is_file()

    rows = service.list_candidates()
    assert [row["id"] for row in rows] == [candidate_id]
    assert rows[0]["title"] == "mul"
    assert rows[0]["status"] == "proposed"
    assert rows[0]["files_changed"] == ["pet-sidecar/gateway/foo.py"]
    assert service.list_candidates(limit=0) == []

    shown = service.show(candidate_id)
    assert shown["goal"] == "add a multiplication helper"
    assert shown["plan"] == "append it"
    assert shown["parent_id"] is None
    assert shown["diff_sha256"]

    discarded = service.discard(candidate_id)
    assert discarded["status"] == "discarded"
    assert not (service.data_dir / "worktrees" / candidate_id).exists()
    # Idempotent: a second discard must not duplicate the lineage event.
    service.discard(candidate_id)

    events = _lineage(service)
    assert [event["event"] for event in events] == ["proposed", "discarded"]
    required = {"ts", "event", "id", "kind", "status", "diff_sha256", "actor"}
    for event in events:
        assert required <= set(event)
        assert event["id"] == candidate_id
    assert events[0]["status"] == "proposed"
    assert events[1]["status"] == "discarded"
    assert events[0]["diff_sha256"] == discarded["diff_sha256"]


def test_unknown_ids_raise_not_found(tmp_path):
    repo = make_repo(tmp_path)
    service = make_service(tmp_path, repo)
    missing = "ev_20000101_000000_abcdef"
    with pytest.raises(NotFound):
        service.show(missing)
    with pytest.raises(NotFound):
        service.evaluate(missing)
    with pytest.raises(NotFound):
        service.discard(missing)
    with pytest.raises(NotFound):
        service.promote(missing, approved_by="tester")
    with pytest.raises(NotFound):
        service.rollback("pr_20000101_000000_abcdef")


def test_inspect_and_read_source(tmp_path):
    repo = make_repo(tmp_path)
    # Untracked on purpose: the live tree is allowed to be dirty; inspect
    # must only report it, and read_source must still refuse secrets.
    write(repo / "pet-sidecar" / "gateway" / ".env", "SECRET=1\n")
    service = make_service(tmp_path, repo)
    record = service.propose(goal="add mul", plan="", edits=[PASSING_EDIT])

    report = service.inspect()
    assert "HEAD " in report
    assert record["base_commit"][:7] in report
    assert "dirty_files=1" in report
    assert "## tests" in report
    assert "foo.py" in report
    assert "candidates: 1" in report
    assert str(service.data_dir) in report

    source = service.read_source("pet-sidecar/gateway/foo.py")
    assert source.splitlines()[0] == "1\tdef add(a, b):"
    assert source.splitlines()[1] == "2\t    return a + b"
    sliced = service.read_source("pet-sidecar/gateway/foo.py", start=2, end=2)
    assert sliced == "2\t    return a + b\n"
    tests_source = service.read_source("pet-sidecar/tests/test_foo.py")
    assert "test_add" in tests_source

    with pytest.raises(NotAllowed):
        service.read_source("pet-sidecar/gateway/.env")
    with pytest.raises(NotAllowed):
        service.read_source(str((tmp_path / "outside.py").resolve()))


def test_env_data_dir_default(tmp_path, monkeypatch):
    repo = make_repo(tmp_path)
    target = tmp_path / "env-evolve"
    monkeypatch.setenv("PET_EVOLVE_DIR", str(target))
    service = EvolveService(repo)
    assert service.data_dir == target.resolve()
    assert service.store.lineage_path == target.resolve() / "lineage.jsonl"
