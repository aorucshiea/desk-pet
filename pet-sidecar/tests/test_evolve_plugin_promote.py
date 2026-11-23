"""Plugin candidates: policy gate, subprocess smoke, promotion, rollback."""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway.evolve.models import NotAllowed, PromotionRefused
from gateway.evolve.service import EvolveService
from _evolve_testkit import PASSING_EDIT, make_repo, make_service, requires_git, write

pytestmark = requires_git

OLD_PLUGIN = (
    '"""old organ"""\n'
    "\n"
    "\n"
    "def apply(ctx):\n"
    '    ctx.tool("old_tool", "old", None, lambda args: "old")\n'
)
NEW_PLUGIN = OLD_PLUGIN.replace('"old_tool", "old"', '"new_tool", "new"')
FRESH_PLUGIN = (
    '"""fresh organ"""\n'
    "\n"
    "\n"
    "def apply(ctx):\n"
    '    ctx.tool("fresh_tool", "fresh", None, lambda args: "fresh")\n'
)


def _reload_ok(calls: list[str], name: str):
    def _reload():
        calls.append(name)
        return {"ok": True, "loaded": [name], "failed": [], "skipped": [], "unloaded": []}

    return _reload


def test_plugin_propose_evaluate_promote_rollback(tmp_path):
    repo = make_repo(tmp_path)
    plugins = tmp_path / "plugins"
    write(plugins / "old_plugin.py", OLD_PLUGIN)
    calls: list[str] = []
    service = make_service(tmp_path, repo, plugin_dir=plugins, reload_plugins=_reload_ok(calls, "old_plugin"))

    record = service.propose(
        goal="rename the old organ's tool",
        plan="swap the registered tool",
        edits=[
            {
                "path": "old_plugin.py",
                "old": '"old_tool", "old"',
                "new": '"new_tool", "new"',
            }
        ],
        kind="plugin",
        title="new tool",
    )
    assert record["kind"] == "plugin"
    assert record["plugin_name"] == "old_plugin.py"
    assert record["worktree"] is None
    # Live file is untouched by propose.
    assert (plugins / "old_plugin.py").read_text(encoding="utf-8") == OLD_PLUGIN

    roster = service.inspect()
    assert "old_plugin.py" in roster
    assert "old_tool" in roster

    with pytest.raises(PromotionRefused):
        service.promote(record["id"], approved_by="tester")

    evaluated = service.evaluate(record["id"])
    assert evaluated["status"] == "verified", evaluated
    assert evaluated["smoke"]["tools"] == ["new_tool"]
    assert evaluated["counts"] == {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}

    promoted = service.promote(record["id"], approved_by="测试用户")
    assert promoted["status"] == "promoted"
    assert calls == ["old_plugin"]
    assert (plugins / "old_plugin.py").read_text(encoding="utf-8") == NEW_PLUGIN

    promotion_id = promoted["promotion_id"]
    promotion = promoted["promotions"][-1]
    backup = promotion["backup"]
    assert backup
    assert (tmp_path / "data" / "backups" / promotion_id / "old_plugin.py").is_file()
    assert Path(backup).read_text(encoding="utf-8") == OLD_PLUGIN
    assert (tmp_path / "data" / "backups" / promotion_id / "promotion.json").is_file()

    rolled = service.rollback(promotion_id)
    assert rolled["status"] == "rolled_back"
    assert calls == ["old_plugin", "old_plugin"]
    assert (plugins / "old_plugin.py").read_text(encoding="utf-8") == OLD_PLUGIN

    # Idempotent-ish: rolling the same promotion back again is safe.
    service.rollback(promotion_id)
    assert calls == ["old_plugin", "old_plugin", "old_plugin"]
    assert (plugins / "old_plugin.py").read_text(encoding="utf-8") == OLD_PLUGIN


def test_new_plugin_and_rollback_removes_it(tmp_path):
    # No plugin kernel copied into this repo: the evaluator must fall back to
    # the installed gateway package for the smoke harness, exactly as a
    # synthetic test repo would require.
    repo = make_repo(tmp_path, plugin_kernel=False)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    calls: list[str] = []
    service = make_service(
        tmp_path, repo, plugin_dir=plugins, reload_plugins=_reload_ok(calls, "fresh_plugin")
    )
    record = service.propose(
        goal="add a fresh organ",
        plan="",
        edits=[{"path": "fresh_plugin.py", "old": "", "new": FRESH_PLUGIN}],
        kind="plugin",
    )
    evaluated = service.evaluate(record["id"])
    assert evaluated["status"] == "verified", evaluated
    assert evaluated["smoke"]["tools"] == ["fresh_tool"]

    promoted = service.promote(record["id"], approved_by="user")
    live = plugins / "fresh_plugin.py"
    assert live.read_text(encoding="utf-8") == FRESH_PLUGIN
    promotion = promoted["promotions"][-1]
    assert promotion["live_existed"] is False
    assert not (tmp_path / "data" / "backups" / promoted["promotion_id"] / "fresh_plugin.py").exists()

    service.rollback(promoted["promotion_id"])
    assert not live.exists()


def test_rollback_of_a_new_plugin_accepts_its_absence_from_the_kernel(tmp_path):
    """The real kernel reports only plugins it loaded. A deleted new plugin is
    absent from ``loaded`` — that is success, not "reload failed"."""
    repo = make_repo(tmp_path, plugin_kernel=False)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    calls: list[str] = []

    def _reload_reflecting_disk():
        calls.append("reload")
        live = plugins / "fresh_plugin.py"
        return {
            "ok": True,
            "loaded": ["fresh_plugin"] if live.exists() else [],
            "failed": [],
            "skipped": [],
            "unloaded": [],
        }

    service = make_service(
        tmp_path, repo, plugin_dir=plugins, reload_plugins=_reload_reflecting_disk
    )
    record = service.propose(
        goal="add a fresh organ",
        plan="",
        edits=[{"path": "fresh_plugin.py", "old": "", "new": FRESH_PLUGIN}],
        kind="plugin",
    )
    assert service.evaluate(record["id"])["status"] == "verified"
    promoted = service.promote(record["id"], approved_by="user")

    rolled = service.rollback(promoted["promotion_id"])
    assert rolled["status"] == "rolled_back"
    assert not (plugins / "fresh_plugin.py").exists()


def test_policy_violation_is_rejected_at_propose(tmp_path):
    repo = make_repo(tmp_path)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    service = make_service(tmp_path, repo, plugin_dir=plugins)
    with pytest.raises(NotAllowed) as excinfo:
        service.propose(
            goal="escape",
            plan="",
            edits=[
                {
                    "path": "evil.py",
                    "old": "",
                    "new": "import os\n\ndef apply(ctx):\n    pass\n",
                }
            ],
            kind="plugin",
        )
    assert "not allowed" in str(excinfo.value)
    assert not (plugins / "evil.py").exists()
    assert not list((tmp_path / "data" / "candidates").iterdir())


def test_plugin_paths_must_be_plain_filenames(tmp_path):
    repo = make_repo(tmp_path)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    service = make_service(tmp_path, repo, plugin_dir=plugins)
    for bad_path in ("../evil.py", "nested/evil.py", "Bad-Name.py", "evil" ):
        with pytest.raises(NotAllowed):
            service.propose(
                goal="bad path",
                plan="",
                edits=[{"path": bad_path, "old": "", "new": "def apply(ctx):\n    pass\n"}],
                kind="plugin",
            )


def test_core_promote_is_review_only(tmp_path):
    repo = make_repo(tmp_path)
    service = make_service(tmp_path, repo)
    record = service.propose(goal="core change", plan="", edits=[PASSING_EDIT])
    with pytest.raises(PromotionRefused) as excinfo:
        service.promote(record["id"], approved_by="tester")
    assert "M1 review-only" in str(excinfo.value)


def test_failed_reload_restores_the_live_plugin(tmp_path):
    repo = make_repo(tmp_path)
    plugins = tmp_path / "plugins"
    write(plugins / "old_plugin.py", OLD_PLUGIN)

    def bad_reload():
        return {"ok": False, "error": "kernel exploded"}

    service = make_service(tmp_path, repo, plugin_dir=plugins, reload_plugins=bad_reload)
    record = service.propose(
        goal="rename",
        plan="",
        edits=[
            {
                "path": "old_plugin.py",
                "old": '"old_tool", "old"',
                "new": '"new_tool", "new"',
            }
        ],
        kind="plugin",
    )
    assert service.evaluate(record["id"])["status"] == "verified"

    with pytest.raises(PromotionRefused) as excinfo:
        service.promote(record["id"], approved_by="tester")
    assert "已恢复旧文件" in str(excinfo.value)
    assert (plugins / "old_plugin.py").read_text(encoding="utf-8") == OLD_PLUGIN
    assert service.show(record["id"])["status"] == "evaluated"
    assert service.show(record["id"])["promotion_id"] is None


def test_plugin_unavailable_without_plugin_dir(tmp_path):
    repo = make_repo(tmp_path)
    service = EvolveService(repo, data_dir=tmp_path / "data")
    with pytest.raises(NotAllowed):
        service.propose(
            goal="plugin",
            plan="",
            edits=[{"path": "x.py", "old": "", "new": "def apply(ctx):\n    pass\n"}],
            kind="plugin",
        )


def test_broken_plugin_smoke_fails(tmp_path):
    repo = make_repo(tmp_path)
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    service = make_service(tmp_path, repo, plugin_dir=plugins)
    record = service.propose(
        goal="broken",
        plan="",
        edits=[
            {
                "path": "broken_plugin.py",
                "old": "",
                "new": "def apply(ctx):\n    raise RuntimeError('plugin boom')\n",
            }
        ],
        kind="plugin",
    )
    result = service.evaluate(record["id"])
    assert result["status"] == "failed"
    assert result["smoke"]["stage"] == "error"
    assert "plugin boom" in result["smoke"]["error"]
