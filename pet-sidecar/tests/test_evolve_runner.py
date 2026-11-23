"""Sandbox runner: env allowlist, timeout tree-kill, output cap."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from gateway.evolve.runner import build_sandbox_env, run_command


def _python() -> str:
    return sys.executable


def test_run_command_success(tmp_path: Path):
    result = run_command(
        [_python(), "-c", "print('hello-evolve')"],
        cwd=tmp_path,
        timeout=30,
        sandbox=tmp_path / "sandbox",
    )
    assert result["ok"] is True
    assert result["returncode"] == 0
    assert result["timed_out"] is False
    assert "hello-evolve" in result["output"]
    assert result["truncated"] is False


def test_run_command_timeout_kills_the_process(tmp_path: Path):
    started = time.monotonic()
    result = run_command(
        [_python(), "-c", "import time; time.sleep(10)"],
        cwd=tmp_path,
        timeout=0.5,
        sandbox=tmp_path / "sandbox",
    )
    elapsed = time.monotonic() - started
    assert result["timed_out"] is True
    assert result["ok"] is False
    # The tree kill must not wait for the sleep to finish.
    assert elapsed < 8


def test_secrets_are_not_inherited(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live-secret")
    monkeypatch.setenv("PET_GATEWAY_TOKEN", "gateway-secret")
    monkeypatch.setenv("DEEPSEEK_TOKEN", "deepseek-secret")
    result = run_command(
        [
            _python(),
            "-c",
            "import os; print(os.environ.get('OPENAI_API_KEY', '<missing>'));"
            " print(os.environ.get('PET_GATEWAY_TOKEN', '<missing>'));"
            " print(os.environ.get('DEEPSEEK_TOKEN', '<missing>'));"
            " print(os.environ.get('GITHUB_TOKEN', '<missing>'));"
            " print(os.environ.get('AZURE_REGION', '<missing>'))",
        ],
        cwd=tmp_path,
        timeout=30,
        env={
            "GITHUB_TOKEN": "ghp-explicit",
            "OPENAI_API_KEY": "sk-explicit",
            "AZURE_REGION": "eastus",
        },
        sandbox=tmp_path / "sandbox",
    )
    assert result["ok"] is True
    output = result["output"]
    assert output.count("<missing>") == 5
    for secret in ("sk-live-secret", "gateway-secret", "deepseek-secret", "ghp-explicit", "sk-explicit"):
        assert secret not in output


def test_sandbox_environment_is_forced(tmp_path: Path):
    sandbox = tmp_path / "sandbox"
    env = build_sandbox_env(
        sandbox,
        {"PET_MEMORY_DIR": str(tmp_path / "elsewhere"), "SOME_SETTING": "yes"},
    )
    assert env["PET_HOME"] == str(sandbox.resolve())
    assert env["PET_MEMORY_DIR"] == str(sandbox.resolve() / "memories")
    assert env["PET_EVOLVE_SANDBOX"] == "1"
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["PYTHONHASHSEED"] == "0"
    assert env["HOME"] == str(sandbox.resolve() / "home")
    assert env["USERPROFILE"] == str(sandbox.resolve() / "home")
    assert env["SOME_SETTING"] == "yes"


def test_output_is_capped_head_and_tail(tmp_path: Path):
    result = run_command(
        [_python(), "-c", "print('H' * 100000); print('T' * 100000)"],
        cwd=tmp_path,
        timeout=30,
        sandbox=tmp_path / "sandbox",
    )
    assert result["ok"] is True
    assert result["truncated"] is True
    output = result["output"]
    assert output.startswith("H")
    assert output.rstrip().endswith("T")
    assert "[truncated" in output
    assert len(output.encode("utf-8")) <= 64 * 1024
