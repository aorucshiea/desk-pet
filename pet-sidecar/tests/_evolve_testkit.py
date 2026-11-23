"""Shared helpers for the evolve tests (not collected by pytest)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

GIT = shutil.which("git")
requires_git = pytest.mark.skipif(GIT is None, reason="git is not available")

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
KERNEL_FILES = ("petplugins.py", "plugin_policy.py", "log_setup.py")

FOO_SOURCE = "def add(a, b):\n    return a + b\n"
TEST_SOURCE = (
    "from gateway.foo import add\n"
    "\n"
    "\n"
    "def test_add():\n"
    "    assert add(1, 2) == 3\n"
)

PASSING_EDIT = {
    "path": "pet-sidecar/gateway/foo.py",
    "old": FOO_SOURCE,
    "new": FOO_SOURCE + "\n\ndef mul(a, b):\n    return a * b\n",
}
BREAKING_EDIT = {
    "path": "pet-sidecar/gateway/foo.py",
    "old": "    return a + b",
    "new": "    return a - b",
}
JS_EDIT = {
    "path": "clawd-on-desk/src/app.js",
    "old": "export const x = 1;",
    "new": "export const x = 2;",
}


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def make_repo(
    tmp_path: Path,
    *,
    failing_test: bool = False,
    js: bool = True,
    plugin_kernel: bool = True,
) -> Path:
    repo = tmp_path / "repo"
    write(repo / "pet-sidecar" / "gateway" / "__init__.py", "")
    write(repo / "pet-sidecar" / "gateway" / "foo.py", FOO_SOURCE)
    test_source = TEST_SOURCE
    if failing_test:
        test_source += (
            "\n\ndef test_baseline_red():\n"
            "    assert False, 'baseline is red'\n"
        )
    write(repo / "pet-sidecar" / "tests" / "test_foo.py", test_source)
    if js:
        write(repo / "clawd-on-desk" / "src" / "app.js", "export const x = 1;\n")
    if plugin_kernel:
        for name in KERNEL_FILES:
            src = PACKAGE_ROOT / "gateway" / name
            dst = repo / "pet-sidecar" / "gateway" / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)

    git(repo, "init")
    git(repo, "config", "user.email", "evolve-test@example.com")
    git(repo, "config", "user.name", "Evolve Test")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "config", "core.longpaths", "true")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "base")
    return repo


def clean_status(repo: Path) -> str:
    return git(repo, "status", "--porcelain").stdout.strip()


def make_service(tmp_path: Path, repo: Path, **kwargs):
    from gateway.evolve.service import EvolveService

    data_dir = kwargs.pop("data_dir", tmp_path / "data")
    return EvolveService(repo, data_dir=data_dir, **kwargs)


__all__ = [
    "BREAKING_EDIT",
    "FOO_SOURCE",
    "GIT",
    "JS_EDIT",
    "PASSING_EDIT",
    "PACKAGE_ROOT",
    "TEST_SOURCE",
    "clean_status",
    "git",
    "make_repo",
    "make_service",
    "requires_git",
    "write",
]
