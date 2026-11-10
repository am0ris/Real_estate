"""Guards for the promises the CRM workbook makes about the rest of the system.

* It is separate from the assistant: nothing in the agents, RAG, retrieval, channels, voice or
  LLM code imports it, and it imports none of them.
* The server can start without its optional packages: the CRM routes load them lazily.
* The packages it needs are declared for production.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "app"
EXCEL = APP / "crm" / "excel"

ASSISTANT_PACKAGES = ("agents", "rag", "retrieval", "channels", "voice", "llm", "workflows")
HEAVY = ("openpyxl", "defusedxml")


def imports_of(path: Path):
    """(module name, is_top_level_statement) for every import in a file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    top = {id(node) for node in tree.body}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, id(node) in top
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module, id(node) in top


def python_files(folder: Path):
    return [path for path in folder.rglob("*.py") if "__pycache__" not in path.parts]


def test_nothing_in_the_assistant_imports_the_crm_workbook():
    offenders = []
    for package in ASSISTANT_PACKAGES:
        for path in python_files(APP / package):
            offenders += [f"{path.relative_to(ROOT)} -> {module}" for module, _ in imports_of(path) if module.startswith("app.crm.excel")]
    assert offenders == []


def test_the_crm_workbook_does_not_import_the_assistant():
    offenders = []
    for path in python_files(EXCEL):
        for module, _ in imports_of(path):
            if any(module == f"app.{package}" or module.startswith(f"app.{package}.") for package in ASSISTANT_PACKAGES):
                offenders.append(f"{path.relative_to(ROOT)} -> {module}")
    assert offenders == []


def test_the_main_application_only_reaches_the_workbook_through_a_lazy_route():
    """app.main -> routes -> crm_admin must not pull in openpyxl, the exporter or the importer at start-up."""
    route = APP / "api" / "routes" / "crm_admin.py"
    startup = [module for module, top_level in imports_of(route) if top_level]
    assert not [m for m in startup if m.startswith(HEAVY)]
    assert not [m for m in startup if m.startswith("app.crm.excel") and m not in ("app.crm.excel.errors", "app.crm.excel.signing")], startup
    lazy = [module for module, top_level in imports_of(route) if not top_level and module.startswith("app.crm.excel")]
    assert {"app.crm.excel.service", "app.crm.excel.importer"} <= set(lazy)


def test_only_the_workbook_modules_import_openpyxl():
    users = {path.name for path in python_files(APP) if any(module.startswith(HEAVY) for module, _ in imports_of(path))}
    assert users <= {"exporter.py", "importer_core.py"}, users


def test_the_light_modules_stay_light():
    """The modules the route imports at start-up must not drag in the heavy ones transitively."""
    for name in ("errors.py", "signing.py", "schema.py"):
        internal = [m for m, _ in imports_of(EXCEL / name) if m.startswith("app.")]
        assert set(internal) <= {"app.crm.excel.schema"}, (name, internal)
        assert not [m for m, _ in imports_of(EXCEL / name) if m.startswith(HEAVY)]


def test_the_packages_the_workbook_needs_are_declared_for_production():
    lines = [line.strip() for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
    for package in ("openpyxl", "defusedxml"):
        assert any(re.match(rf"{package}==\d", line) for line in lines), f"{package} is not pinned in requirements.txt"
