"""Layering rules from docs/ARCHITECTURE.md, checked on every import in app/ (including
imports inside functions). If this fails, move the code to the right layer instead of
adding an exception."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app"

LOW = ("app.core", "app.models")
INFRA = ("app.llm", "app.sources", "app.jobs", "app.ranking", "app.documents")
UPPER = ("app.services", "app.features", "app.web", "app.bot", "app.plugins", "app.main", "app.cli")

# (module prefix, forbidden import prefixes, allowed exceptions)
RULES = [
    ("app.core", INFRA + UPPER, ("app.models",)),
    ("app.models", INFRA + UPPER, ()),
    ("app.llm", UPPER, ()),
    ("app.sources", UPPER, ()),
    ("app.jobs", UPPER, ()),
    ("app.ranking", UPPER, ()),
    ("app.documents", UPPER, ()),
    ("app.features", ("app.services", "app.web", "app.bot"), ()),
    ("app.services", ("app.web", "app.bot"), ()),
    # Interfaces talk to services/jobs, not to LLM providers or source fetchers.
    ("app.web", ("app.bot.runner", "app.bot.handlers", "app.llm.providers", "app.llm.gateway", "app.sources.web"), ()),
    ("app.bot", ("app.web", "app.llm.providers", "app.llm.gateway", "app.sources"), ()),
]


def _module_name(path: Path) -> str:
    rel = path.relative_to(APP.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.append(node.module)
            found += [f"{node.module}.{a.name}" for a in node.names]
    return found


def _matches(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


MODULES = sorted(APP.rglob("*.py"))


@pytest.mark.parametrize("path", MODULES, ids=lambda p: str(p.relative_to(APP)))
def test_layering(path):
    module = _module_name(path)
    violations = []
    for prefix, forbidden, allowed in RULES:
        if not _matches(module, prefix):
            continue
        for imp in _imports(path):
            if any(_matches(imp, a) for a in allowed):
                continue
            if any(_matches(imp, f) for f in forbidden):
                violations.append(imp)
    assert not violations, f"{module} imports {sorted(set(violations))}"


@pytest.mark.parametrize("path", sorted((APP / "features").glob("*/__init__.py")),
                         ids=lambda p: p.parent.name)
def test_features_do_not_import_each_other(path):
    me = path.parent.name
    feature_dirs = {p.name for p in (APP / "features").iterdir() if p.is_dir() and not p.name.startswith("_")}
    others = {imp.split(".")[2] for imp in _imports(path)
              if imp.startswith("app.features.") and imp.split(".")[2] in feature_dirs} - {me}
    assert not others, f"feature {me} imports other features: {sorted(others)}"
