"""Shared fixtures for LumiSafe tests.

* Every test gets an isolated ``LUMISAFE_HOME`` so nothing touches ``~/.lumisafe``.
* Unit tests may not open network connections (SPEC §3 rule 6). Only tests
  marked ``integration`` may touch Yahoo data or Alpaca paper.
"""

from __future__ import annotations

import socket

import pytest


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: may touch Yahoo data or Alpaca paper (run with -m integration)"
    )


def pytest_collection_modifyitems(config, items):
    """Integration tests are opt-in: they only run when ``-m`` mentions ``integration``."""
    if "integration" in (config.option.markexpr or ""):
        return
    skip = pytest.mark.skip(reason="integration test: run with -m integration")
    for item in items:
        if item.get_closest_marker("integration"):
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _isolated_lumisafe_home(tmp_path, monkeypatch):
    home = tmp_path / "lumisafe_home"
    monkeypatch.setenv("LUMISAFE_HOME", str(home))
    monkeypatch.delenv("LUMISAFE_LIVE", raising=False)
    yield home


@pytest.fixture(autouse=True)
def _block_network(request, monkeypatch):
    if request.node.get_closest_marker("integration"):
        yield
        return

    def _guard(*args, **kwargs):
        raise RuntimeError("Network access is not allowed in LumiSafe unit tests (SPEC §3 rule 6)")

    monkeypatch.setattr(socket.socket, "connect", _guard)
    monkeypatch.setattr(socket, "create_connection", _guard)
    yield
