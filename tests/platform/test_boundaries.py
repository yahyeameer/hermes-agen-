"""Architectural guardrails.

These tests fail when the architecture erodes, which is the failure mode that matters
most: every individual violation looks reasonable in isolation and is expensive to undo
once it has spread.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NOVA = ROOT / "nova"

#: Runtime vocabulary that must not leak into NOVA's own interfaces.
RUNTIME_WORDS = ("hermes", "profile", "soul", "kanban", "cordis", "dsh")


def nova_python_files() -> list[Path]:
    return [p for p in NOVA.rglob("*.py") if "__pycache__" not in p.parts]


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module.split(".")[0])
    return names


def test_platform_layer_never_imports_the_runtime():
    """NOVA must load without the runtime installed. The adapter uses on-disk seams only."""
    runtime_packages = {
        "hermes_cli", "hermes_constants", "hermes_state", "hermes_logging",
        "agent", "gateway", "tools", "toolsets", "cli", "run_agent", "model_tools",
    }
    offenders: list[str] = []
    for path in nova_python_files():
        leaked = imported_modules(path) & runtime_packages
        if leaked:
            offenders.append(f"{path.relative_to(ROOT)}: imports {', '.join(sorted(leaked))}")
    assert not offenders, "platform code imported the runtime:\n" + "\n".join(offenders)


def test_platform_layer_depends_only_on_stdlib_and_yaml():
    """Keeping the dependency surface at zero is what makes the layer portable."""
    allowed_third_party = {"yaml"}
    stdlib = set(sys.stdlib_module_names)
    offenders: list[str] = []
    for path in nova_python_files():
        for name in imported_modules(path):
            if name in stdlib or name in allowed_third_party or name == "nova":
                continue
            offenders.append(f"{path.relative_to(ROOT)}: imports {name}")
    assert not offenders, "unexpected dependency:\n" + "\n".join(offenders)


def test_runtime_contract_is_free_of_runtime_vocabulary():
    """The adapter interface must be expressed in NOVA's words, not one runtime's."""
    source = (NOVA / "runtime" / "base.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            lowered = node.name.lower()
            for word in RUNTIME_WORDS:
                if word in lowered:
                    offenders.append(f"{node.name} (line {node.lineno}) contains {word!r}")
        elif isinstance(node, ast.arg):
            lowered = node.arg.lower()
            for word in RUNTIME_WORDS:
                if word in lowered:
                    offenders.append(f"argument {node.arg} (line {node.lineno}) contains {word!r}")
    assert not offenders, "runtime vocabulary leaked into the contract:\n" + "\n".join(offenders)


def code_without_prose(path: Path) -> str:
    """Source with docstrings and comments removed.

    Prose may name the runtime — explaining why a boundary exists requires it. Code may
    not: an identifier or a string literal naming the runtime outside the adapter is
    knowledge that has escaped its package.
    """
    import tokenize

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)

    out: list[str] = []
    with open(path, "rb") as handle:
        for token in tokenize.tokenize(handle.readline):
            if token.type == tokenize.COMMENT:
                continue
            if token.type == tokenize.STRING:
                try:
                    value = ast.literal_eval(token.string)
                except (ValueError, SyntaxError):
                    value = None
                if isinstance(value, str) and value in docstrings:
                    continue
            out.append(token.string)
    return "\n".join(out)


def test_only_the_adapter_package_names_the_runtime_in_code():
    """Runtime knowledge is confined to one package, so a second adapter is additive."""
    offenders: list[str] = []
    for path in nova_python_files():
        rel = path.relative_to(ROOT).as_posix()
        if "runtime/hermes" in rel or rel.endswith("nova/runtime/registry.py"):
            continue
        if "hermes" in code_without_prose(path).lower():
            offenders.append(rel)
    assert not offenders, (
        "'hermes' appears in code outside the adapter package:\n" + "\n".join(offenders)
    )


def test_registry_names_the_runtime_only_inside_the_registration_hook():
    """The registry is the one seam allowed to know a bundled adapter's name.

    Structural rather than a count: every mention must sit inside the lazy-registration
    function, so the name cannot leak into the registry's general logic.
    """
    path = NOVA / "runtime" / "registry.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    hook = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_ensure_builtins"
    )
    hook_lines = set(range(hook.lineno, (hook.end_lineno or hook.lineno) + 1))

    # Docstrings are prose and may name the runtime; exclude their line ranges.
    prose_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                literal = body[0].value
                if isinstance(literal.value, str):
                    prose_lines.update(
                        range(literal.lineno, (literal.end_lineno or literal.lineno) + 1)
                    )

    outside: list[int] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        code = line.split("#", 1)[0]
        if "hermes" in code.lower() and number not in hook_lines and number not in prose_lines:
            outside.append(number)

    assert not outside, (
        f"the runtime name appears in registry code outside _ensure_builtins at line(s) "
        f"{outside}"
    )


def test_no_toplevel_platform_package_shadowing_stdlib():
    """A root 'platform' package breaks platform.system() in 40 upstream modules."""
    assert not (ROOT / "platform" / "__init__.py").exists()


def test_upstream_code_never_imports_nova():
    """The inverted dependency would make every upstream merge a conflict."""
    offenders: list[str] = []
    skip = {".git", "node_modules", "__pycache__", "apps", "website", ".venv"}
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if any(part in skip for part in rel.parts):
            continue
        if rel.as_posix().startswith(("nova/", "tests/platform/", "scripts/check_protected")):
            continue
        try:
            names = imported_modules(path)
        except SyntaxError:
            continue
        if "nova" in names:
            offenders.append(rel.as_posix())
    assert not offenders, "upstream code imports nova:\n" + "\n".join(offenders)


def test_protected_identifier_script_passes():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_protected_identifiers.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr


def test_protected_identifier_script_catches_a_stdlib_shadow(tmp_path):
    """The guardrail must actually fire, not just pass vacuously."""
    shadow = ROOT / "platform" / "__init__.py"
    shadow.parent.mkdir(exist_ok=True)
    shadow.write_text("# temporary\n", encoding="utf-8")
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_protected_identifiers.py")],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        assert result.returncode == 1
        assert "shadows the stdlib" in result.stderr
    finally:
        shadow.unlink()
        if not any(shadow.parent.iterdir()):
            shadow.parent.rmdir()


@pytest.mark.parametrize("module", ["nova", "nova.spec", "nova.runtime", "nova.audit", "nova.apply"])
def test_public_modules_import_cleanly(module):
    __import__(module)
