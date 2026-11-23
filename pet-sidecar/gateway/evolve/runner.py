"""Sandboxed subprocess runner for candidate evaluation.

The environment is rebuilt from an allowlist instead of inherited: the
gateway holds provider keys and a local API token, and candidate code (or
tests it causes to run) must never see them. HOME/USERPROFILE are pinned
into the per-candidate sandbox for the same reason.

A timeout kills the whole process tree. pytest may start workers/children;
leaving one alive would keep writing into the worktree after the verdict.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Sequence

BASE_ENV_KEYS = (
    "PATH",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "NUMBER_OF_PROCESSORS",
    "USERPROFILE",
    "HOME",
)

SECRET_MARKERS = (
    "TOKEN",
    "KEY",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
)

# Provider namespaces are refused even when the variable does not contain
# "KEY"/"TOKEN" (e.g. AZURE_REGION, DEEPSEEK_BASE_URL): they are ambient
# credentials/configuration for a service a candidate must not talk to.
BLOCKED_PREFIXES = (
    "AWS_",
    "AZURE_",
    "DEEPSEEK_",
    "ANTHROPIC_",
    "OPENAI_",
    "HF_",
    "HUGGINGFACE_",
)
BLOCKED_NAMES = {"PET_GATEWAY_TOKEN", "GITHUB_TOKEN"}

OUTPUT_LIMIT = 64 * 1024
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _looks_secret(name: str) -> bool:
    upper = name.upper()
    if upper in BLOCKED_NAMES or upper.startswith(BLOCKED_PREFIXES):
        return True
    return any(marker in upper for marker in SECRET_MARKERS)


def build_sandbox_env(
    sandbox: Path | str | None = None,
    overrides: dict[str, Any] | None = None,
) -> dict[str, str]:
    overrides = dict(overrides or {})
    if sandbox is None:
        raw = overrides.get("PET_HOME") or overrides.get("HOME") or ""
        if raw:
            sandbox_path = Path(str(raw))
        else:
            sandbox_path = Path(tempfile.gettempdir()) / "pet-evolve-sandbox"
    else:
        sandbox_path = Path(sandbox)
    sandbox_path = sandbox_path.expanduser().resolve()

    env: dict[str, str] = {}
    for key in BASE_ENV_KEYS:
        if key in os.environ:
            env[key] = os.environ[key]

    # Explicit overrides are still filtered: passing a provider key through
    # this API must not become a way around the allowlist.
    for key, value in overrides.items():
        if not isinstance(key, str) or _looks_secret(key):
            continue
        env[key] = str(value)

    home = sandbox_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (sandbox_path / "memories").mkdir(parents=True, exist_ok=True)
    (sandbox_path / "logs").mkdir(parents=True, exist_ok=True)

    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["PET_HOME"] = str(sandbox_path)
    # These are forced, not defaulted: an evaluation must never be able to
    # point memory/log/cache writes back at the live profile or worktree.
    env["PET_MEMORY_DIR"] = str(sandbox_path / "memories")
    env["PET_LOG_DIR"] = str(sandbox_path / "logs")
    env["PYTHONPYCACHEPREFIX"] = str(sandbox_path / "pycache")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONHASHSEED"] = "0"
    env["PET_EVOLVE_SANDBOX"] = "1"
    return env


def _cap_output(data: bytes) -> tuple[str, bool]:
    if len(data) <= OUTPUT_LIMIT:
        return data.decode("utf-8", errors="replace"), False
    dropped = len(data) - OUTPUT_LIMIT
    note = f"\n...[truncated {dropped} bytes]...\n"
    # Keep the *encoded result* within 64 KiB, marker included.
    budget = max(2, OUTPUT_LIMIT - len(note.encode("utf-8")))
    head_budget = budget // 2
    tail_budget = budget - head_budget
    head = data[:head_budget].decode("utf-8", errors="replace")
    tail = data[-tail_budget:].decode("utf-8", errors="replace")
    return head + note + tail, True


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                creationflags=CREATE_NO_WINDOW,
                shell=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except OSError:
            pass


def run_command(
    argv: Sequence[str],
    cwd: Path | str,
    timeout: float = 300.0,
    env: dict[str, Any] | None = None,
    *,
    sandbox: Path | str | None = None,
) -> dict[str, Any]:
    """Run argv without a shell, kill the tree on timeout, cap the output."""
    args = [str(part) for part in argv]
    run_env = build_sandbox_env(sandbox, env)
    started = time.monotonic()

    try:
        proc = subprocess.Popen(
            args,
            cwd=str(cwd),
            env=run_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
            start_new_session=(os.name != "nt"),
            shell=False,
        )
    except FileNotFoundError:
        return {
            "ok": False,
            "returncode": 127,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "output": f"[命令不存在: {args[0] if args else ''}]",
            "timed_out": False,
            "truncated": False,
        }
    except OSError as exc:
        return {
            "ok": False,
            "returncode": 127,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "output": f"[命令启动失败: {exc}]",
            "timed_out": False,
            "truncated": False,
        }

    timed_out = False
    raw = b""
    try:
        raw, _ = proc.communicate(timeout=max(0.05, float(timeout)))
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            raw, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            raw = b""
    duration_ms = int((time.monotonic() - started) * 1000)
    output, truncated = _cap_output(raw or b"")
    returncode = proc.returncode if proc.returncode is not None else -1
    return {
        "ok": (not timed_out) and returncode == 0,
        "returncode": returncode,
        "duration_ms": duration_ms,
        "output": output,
        "timed_out": timed_out,
        "truncated": truncated,
    }


__all__ = ["BASE_ENV_KEYS", "build_sandbox_env", "run_command"]
