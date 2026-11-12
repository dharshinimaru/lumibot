"""Phase 0: the skeleton imports and the network guard is active."""

import importlib
import pkgutil
import socket

import pytest

import lumisafe


def test_every_lumisafe_module_imports():
    names = [m.name for m in pkgutil.walk_packages(lumisafe.__path__, prefix="lumisafe.")]
    names = [n for n in names if n != "lumisafe.__main__"]  # __main__ would run the CLI
    assert "lumisafe.risk.gate" in names
    for name in names:
        importlib.import_module(name)


def test_unit_tests_cannot_open_network_connections():
    with pytest.raises(RuntimeError, match="Network access is not allowed"):
        socket.create_connection(("example.com", 80), timeout=1)


def test_only_the_bridge_imports_lumibot():
    """SPEC §3 rule 1: all coupling to Lumibot lives in adapters/lumibot_bridge.py."""
    import ast
    from pathlib import Path

    root = Path(lumisafe.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        if path.name == "lumibot_bridge.py":
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module]
            if any(m == "lumibot" or m.startswith("lumibot.") for m in mods):
                offenders.append(str(path.relative_to(root)))
    assert offenders == []
