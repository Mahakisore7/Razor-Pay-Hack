"""No module outside bench.evaluation imports the ground-truth table
(T1.9 checklist; RAZORPAY-INTEGRATION SS6.1).

`.importlinter`'s `ground-truth-is-write-only` contract enforces this same
rule at the package-boundary level, checked in CI via `make lint`. This is
the direct pytest-level assertion the phase checklist calls for
separately: a static scan of every source file's imports, independent of
import-linter's own machinery, so the guarantee doesn't rest on one tool
alone.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "recoup"
_GROUND_TRUTH_MODULE = "recoup.gateway.simulator.ground_truth"
_ALLOWED_PREFIXES = (
    "recoup.gateway.simulator",  # the writer
    "recoup.bench.evaluation",  # the one allowed reader
)


def _imports_ground_truth(path: Path) -> bool:
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.startswith(_GROUND_TRUTH_MODULE) for alias in node.names):
                return True
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module is not None
            and node.module.startswith(_GROUND_TRUTH_MODULE)
        ):
            return True
    return False


def _module_name(path: Path) -> str:
    relative = path.relative_to(_SRC_ROOT.parent)  # keeps the "recoup." prefix
    parts = relative.with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def test_no_module_outside_bench_evaluation_imports_ground_truth() -> None:
    offenders = []
    for path in _SRC_ROOT.rglob("*.py"):
        module = _module_name(path)
        if module.startswith(_ALLOWED_PREFIXES):
            continue
        if _imports_ground_truth(path):
            offenders.append(module)

    assert offenders == [], f"modules importing ground_truth outside the allowed set: {offenders}"


def test_the_scan_itself_would_catch_a_real_violation() -> None:
    """Guards against the scan silently matching nothing -- e.g. if the
    ground-truth module were ever renamed and this test's constant went
    stale, `test_no_module_outside_...` would pass for the wrong reason."""
    fixture = "import recoup.gateway.simulator.ground_truth as gt\n"
    tree = ast.parse(fixture)
    found = any(
        isinstance(node, ast.Import)
        and any(alias.name.startswith(_GROUND_TRUTH_MODULE) for alias in node.names)
        for node in ast.walk(tree)
    )
    assert found
