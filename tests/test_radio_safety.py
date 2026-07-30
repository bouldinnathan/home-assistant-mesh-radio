"""Repository-level guardrails for radio operations that must stay manual."""

from __future__ import annotations

import ast
from pathlib import Path

RUNTIME_PATHS = tuple(
    path
    for path in sorted(Path("custom_components/meshnet").rglob("*"))
    if path.is_file() and path.suffix in {".js", ".json", ".py", ".yaml"}
)


def test_manual_traceroute_is_admin_websocket_only_with_no_background_callsite() -> None:
    """Only the explicit WS handler and bounded delegation chain may call it."""
    websocket_path = Path("custom_components/meshnet/websocket_api.py")
    tree = ast.parse(websocket_path.read_text(encoding="utf-8"))
    handlers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(value, ast.Constant) and value.value == "meshnet/traceroute"
            for value in ast.walk(node)
        )
    ]
    assert len(handlers) == 1
    decorator_names = {
        ast.unparse(decorator)
        for decorator in handlers[0].decorator_list
    }
    assert any(name.endswith("require_admin") for name in decorator_names)

    services = Path("custom_components/meshnet/services.yaml").read_text(
        encoding="utf-8"
    )
    integration = Path("custom_components/meshnet/__init__.py").read_text(
        encoding="utf-8"
    )
    assert "traceroute:" not in services
    assert "SERVICE_TRACEROUTE" not in integration

    allowed_call_owners = {
        handlers[0].name,
        "async_manual_traceroute",
        "_async_manual_traceroute",
    }
    for path in RUNTIME_PATHS:
        if path.suffix != ".py":
            continue
        module = ast.parse(path.read_text(encoding="utf-8"))
        for owner in (
            node
            for node in ast.walk(module)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            for call in (node for node in ast.walk(owner) if isinstance(node, ast.Call)):
                target = call.func
                called_name = (
                    target.attr
                    if isinstance(target, ast.Attribute)
                    else target.id
                    if isinstance(target, ast.Name)
                    else ""
                )
                if called_name == "async_manual_traceroute":
                    assert owner.name in allowed_call_owners, (path, owner.name)


def test_future_manual_traceroute_cooldown_is_documented() -> None:
    """Keep the one-hour backend cooldown invariant visible to maintainers."""
    architecture = Path("docs/ARCHITECTURE.md").read_text(encoding="utf-8")
    usage = Path("docs/USAGE.md").read_text(encoding="utf-8")

    assert "one manual traceroute across the integration every 3,600 seconds" in " ".join(
        architecture.split()
    )
    assert "one manual traceroute across the entire MeshNet integration per hour" in " ".join(
        usage.split()
    )
