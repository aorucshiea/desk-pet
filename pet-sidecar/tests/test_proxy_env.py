"""Gateway boot is robust to shell-style IPv6 NO_PROXY entries."""

from __future__ import annotations

import os

from gateway import server as server_mod


def test_sanitize_no_proxy_unwraps_bracketed_ipv6(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,[::1],127.0.0.1")
    server_mod._sanitize_no_proxy_env()
    assert os.environ["NO_PROXY"] == "localhost,::1,127.0.0.1"


def test_sanitize_no_proxy_leaves_plain_values_alone(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1")
    server_mod._sanitize_no_proxy_env()
    assert os.environ["NO_PROXY"] == "localhost,127.0.0.1"
