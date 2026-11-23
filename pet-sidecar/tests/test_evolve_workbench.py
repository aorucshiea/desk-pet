"""Core workbench: isolated worktree, diff, and strict path guards."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.evolve.models import NotAllowed, WorkbenchError
from _evolve_testkit import (
    FOO_SOURCE,
    PASSING_EDIT,
    clean_status,
    git,
    make_repo,
    make_service,
    requires_git,
)

pytestmark = requires_git


def _candidate_dir(service, candidate_id: str) -> Path:
    return service.data_dir / "candidates" / candidate_id


def test_propose_creates_worktree_and_diff_without_touching_live_tree(tmp_path):
    repo = make_repo(tmp_path)
    service = make_service(tmp_path, repo)
    record = service.propose(goal="add mul", plan="append helper", edits=[PASSING_EDIT])

    worktree = Path(record["worktree"])
    assert worktree.is_dir()
    assert record["status"] == "proposed"
    assert record["base_dirty"] is False
    assert record["kind"] == "core"
    assert record["files_changed"] == ["pet-sidecar/gateway/foo.py"]
    assert record["base_commit"] == git(repo, "rev-parse", "HEAD").stdout.strip()

    diff = (_candidate_dir(service, record["id"]) / "diff.patch").read_text(encoding="utf-8")
    assert "+def mul(a, b):" in diff
    assert "+    return a * b" in diff
    assert record["diff_sha256"] == __import__("hashlib").sha256(diff.encode("utf-8")).hexdigest()

    assert "def mul" in (worktree / "pet-sidecar/gateway/foo.py").read_text(encoding="utf-8")
    # The user's live file is never edited at propose time.
    assert (repo / "pet-sidecar/gateway/foo.py").read_text(encoding="utf-8") == FOO_SOURCE
    assert clean_status(repo) == ""

    # The record holds the full lineage data, and the branch is detached-from-
    # base but named after the candidate.
    payload = json.loads(
        (_candidate_dir(service, record["id"]) / "candidate.json").read_text(encoding="utf-8")
    )
    assert payload["id"] == record["id"]
    assert payload["edits"] == [PASSING_EDIT]
    branch = git(worktree, "branch", "--show-current").stdout.strip()
    assert branch == f"evolve/{record['id']}"

    service.discard(record["id"])
    assert not worktree.exists()
    assert clean_status(repo) == ""
    assert "evolve/" not in git(repo, "branch", "--list", "evolve/*").stdout


@pytest.mark.parametrize(
    "path",
    [
        "../outside.py",
        "pet-sidecar/gateway/../../outside.py",
        "pet-sidecar/tests/test_foo.py",
        "pet-sidecar/gateway/.env",
        "pet-sidecar/gateway/.env.local",
        "clawd-on-desk/src/node_modules/pkg/index.js",
        "pet-sidecar/gateway/foo.txt",
    ],
)
def test_path_guards_reject_unsafe_paths(tmp_path, path):
    repo = make_repo(tmp_path)
    service = make_service(tmp_path, repo)
    with pytest.raises(NotAllowed):
        service.propose(
            goal="unsafe",
            plan="",
            edits=[{"path": path, "old": "", "new": "print('x')\n"}],
        )
    worktrees = service.data_dir / "worktrees"
    assert not list(worktrees.iterdir()) if worktrees.exists() else True
    candidates = service.data_dir / "candidates"
    assert not list(candidates.iterdir()) if candidates.exists() else True


def test_absolute_path_is_rejected(tmp_path):
    repo = make_repo(tmp_path)
    service = make_service(tmp_path, repo)
    absolute = str((repo / "pet-sidecar/gateway/foo.py").resolve())
    with pytest.raises(NotAllowed):
        service.propose(
            goal="absolute",
            plan="",
            edits=[{"path": absolute, "old": "return a + b", "new": "return a - b"}],
        )


def test_old_mismatch_fails_cleanly(tmp_path):
    repo = make_repo(tmp_path)
    service = make_service(tmp_path, repo)
    with pytest.raises(WorkbenchError) as excinfo:
        service.propose(
            goal="stale snippet",
            plan="",
            edits=[
                {
                    "path": "pet-sidecar/gateway/foo.py",
                    "old": "def this_does_not_exist():\n    pass\n",
                    "new": "def replacement():\n    pass\n",
                }
            ],
        )
    assert "出现 0 次" in str(excinfo.value)
    worktrees = service.data_dir / "worktrees"
    assert not list(worktrees.iterdir()) if worktrees.exists() else True
    candidates = service.data_dir / "candidates"
    assert not list(candidates.iterdir()) if candidates.exists() else True
    assert clean_status(repo) == ""


def test_new_file_creation_and_no_deletion(tmp_path):
    repo = make_repo(tmp_path)
    service = make_service(tmp_path, repo)
    record = service.propose(
        goal="new module",
        plan="",
        edits=[
            {
                "path": "pet-sidecar/gateway/extra.py",
                "old": "",
                "new": "def triple(value):\n    return value * 3\n",
            }
        ],
    )
    assert record["files_changed"] == ["pet-sidecar/gateway/extra.py"]
    created = Path(record["worktree"]) / "pet-sidecar/gateway/extra.py"
    assert "def triple" in created.read_text(encoding="utf-8")
    diff = (service.data_dir / "candidates" / record["id"] / "diff.patch").read_text(
        encoding="utf-8"
    )
    assert "new file mode" in diff

    # Creating over an existing file is not allowed.
    with pytest.raises(NotAllowed):
        service.propose(
            goal="duplicate",
            plan="",
            edits=[
                {
                    "path": "pet-sidecar/gateway/foo.py",
                    "old": "",
                    "new": "def other():\n    return 1\n",
                }
            ],
        )
    # Deletion/truncation is not allowed in M1.
    with pytest.raises(NotAllowed):
        service.propose(
            goal="delete",
            plan="",
            edits=[{"path": "pet-sidecar/gateway/foo.py", "old": "def add", "new": ""}],
        )
