"""Evaluation: focused tests, compile gate, baseline comparison, verdicts."""

from __future__ import annotations

import pytest

from _evolve_testkit import (
    BREAKING_EDIT,
    JS_EDIT,
    PASSING_EDIT,
    make_repo,
    make_service,
    requires_git,
)

pytestmark = requires_git


def test_passing_candidate_is_verified_and_full_mode_works(tmp_path):
    repo = make_repo(tmp_path)
    service = make_service(tmp_path, repo)
    record = service.propose(goal="add mul", plan="", edits=[PASSING_EDIT])

    result = service.evaluate(record["id"], full=True)
    assert result["full"] is True
    assert result["status"] == "verified"
    assert result["reason"] == ""
    assert result["tests"] == ["(full suite)"]
    assert result["counts"]["passed"] >= 1
    assert result["counts"]["failed"] == 0
    assert result["counts"]["errors"] == 0
    assert result["new_failures"] == []
    assert result["baseline"]["status"] == "passed"
    assert result["baseline"]["counts"]["passed"] == result["counts"]["passed"]
    assert "passed" in result["log_tail"]

    saved = service.show(record["id"])
    assert saved["status"] == "evaluated"
    assert saved["eval_status"] == "verified"
    assert saved["eval"]["status"] == "verified"
    assert len(saved["evaluations"]) == 1


def test_breaking_candidate_fails_with_new_failure(tmp_path):
    repo = make_repo(tmp_path)
    service = make_service(tmp_path, repo)
    record = service.propose(goal="break math", plan="", edits=[BREAKING_EDIT])

    result = service.evaluate(record["id"])
    assert result["status"] == "failed"
    assert result["tests"] == ["tests/test_foo.py"]
    assert result["counts"]["failed"] >= 1
    assert "tests/test_foo.py::test_add" in result["new_failures"]
    assert result["baseline"]["status"] == "passed"
    assert "FAILED" in result["log_tail"]


def test_syntax_error_fails_at_compile(tmp_path):
    repo = make_repo(tmp_path)
    service = make_service(tmp_path, repo)
    record = service.propose(
        goal="broken syntax",
        plan="",
        edits=[
            {
                "path": "pet-sidecar/gateway/foo.py",
                "old": "    return a + b",
                "new": "    return a +",
            }
        ],
    )
    result = service.evaluate(record["id"])
    assert result["status"] == "failed"
    assert result["reason"] == "compile_error"
    assert result["tests"] == []
    assert result["baseline"] is None
    assert "foo.py" in result["log_tail"]


def test_non_python_change_requires_manual_review(tmp_path):
    repo = make_repo(tmp_path)
    service = make_service(tmp_path, repo)
    record = service.propose(goal="tweak JS", plan="", edits=[JS_EDIT])

    result = service.evaluate(record["id"])
    assert result["status"] == "unverified"
    assert result["reason"] == "manual_review_required"
    assert result["tests"] == []
    assert result["baseline"] is None
    assert "app.js" in result["log_tail"]


def test_red_baseline_still_compares_new_failures(tmp_path):
    repo = make_repo(tmp_path, failing_test=True)
    service = make_service(tmp_path, repo)
    record = service.propose(goal="break math on red base", plan="", edits=[BREAKING_EDIT])

    result = service.evaluate(record["id"])
    assert result["status"] == "failed"
    assert result["baseline"]["status"] == "failed"
    assert "tests/test_foo.py::test_baseline_red" in result["baseline"]["failed_tests"]
    assert "tests/test_foo.py::test_add" in result["new_failures"]
    # The pre-existing red test is not blamed on the candidate.
    assert "tests/test_foo.py::test_baseline_red" not in result["new_failures"]
