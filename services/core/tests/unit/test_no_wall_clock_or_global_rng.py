"""Two of ENGINEERING-STANDARDS SS1's non-negotiables, enforced here by a
static scan rather than a native ruff rule: ruff has no mechanism to ban a
specific method call (`datetime.now()`) scoped to only some packages, and
its import-level `banned-api` can't see `numpy.random` reached through
`import numpy as np; np.random.x()` -- there is no `numpy.random` import
statement for it to match, just an attribute chain on the `numpy` name.

Rule 2: `domain`, `detection`, and `policy` never call `datetime.now()` /
`.utcnow()` -- the clock is injected (`recoup.platform.clock`).
Rule 3: no global RNG anywhere in `recoup` -- `random`'s and
`numpy.random`'s module-level functions carry implicit shared state that
breaks reproducibility the same way an unseeded call would. An explicit
instance (`random.Random(seed)`, `numpy.random.Generator`/`default_rng`)
is the correct alternative and is allowed.

This is a best-effort scan for the idiomatic import styles actually used
in this codebase (`import random`, `import numpy as np`) -- it does not
try to catch every possible obfuscation (e.g. `from random import
random as rnd; rnd()`), the same scope `test_ground_truth_boundary.py`'s
scanner accepts.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "recoup"
_CLOCK_SCOPED_PACKAGES = ("domain", "detection", "policy")
_NUMPY_ALIASES = {"numpy", "np"}
_RANDOM_MODULE_ALLOWED = {"Random"}  # random.Random(seed) is an explicit instance, not global state
_NUMPY_RANDOM_ALLOWED = {
    "Generator",
    "default_rng",
    "PCG64",
    "PCG64DXSM",
    "Philox",
    "SFC64",
    "MT19937",
    "SeedSequence",
}


def _attribute_chain(node: ast.expr) -> list[str] | None:
    """`['datetime', 'now']` for `datetime.now`, `['np', 'random', 'seed']`
    for `np.random.seed`, or `None` for anything that isn't a dotted
    attribute chain rooted at a bare name."""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    parts.append(current.id)
    parts.reverse()
    return parts


def _scan(path: Path, *, is_clock_scoped: bool) -> list[str]:
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    violations = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        chain = _attribute_chain(node.func)
        if chain is None or len(chain) < 2:
            continue

        if is_clock_scoped and chain[-2] == "datetime" and chain[-1] in ("now", "utcnow"):
            violations.append(f"{path}:{node.lineno}: calls datetime.{chain[-1]}()")

        if chain[0] == "random" and chain[1] not in _RANDOM_MODULE_ALLOWED:
            violations.append(f"{path}:{node.lineno}: calls random.{chain[1]}() (global RNG state)")

        if (
            chain[0] in _NUMPY_ALIASES
            and len(chain) >= 3
            and chain[1] == "random"
            and chain[2] not in _NUMPY_RANDOM_ALLOWED
        ):
            violations.append(
                f"{path}:{node.lineno}: calls {chain[0]}.random.{chain[2]}() (global RNG state)"
            )

    return violations


def test_no_wall_clock_or_global_rng_usage() -> None:
    violations = []
    for path in _SRC_ROOT.rglob("*.py"):
        top_level_package = path.relative_to(_SRC_ROOT).parts[0]
        is_clock_scoped = top_level_package in _CLOCK_SCOPED_PACKAGES
        violations.extend(_scan(path, is_clock_scoped=is_clock_scoped))
    assert violations == [], "\n".join(violations)


# --- Meta-tests: the scan itself, exercised directly against fixture files ---


def test_the_scan_catches_datetime_now_when_clock_scoped(tmp_path: Path) -> None:
    fixture = tmp_path / "case.py"
    fixture.write_text("import datetime\ndatetime.now()\n")
    violations = _scan(fixture, is_clock_scoped=True)
    assert len(violations) == 1
    assert "datetime.now" in violations[0]


def test_the_scan_catches_datetime_utcnow_when_clock_scoped(tmp_path: Path) -> None:
    fixture = tmp_path / "case.py"
    fixture.write_text("import datetime\ndatetime.utcnow()\n")
    violations = _scan(fixture, is_clock_scoped=True)
    assert len(violations) == 1


def test_the_scan_ignores_datetime_now_when_not_clock_scoped(tmp_path: Path) -> None:
    fixture = tmp_path / "clock.py"
    fixture.write_text("import datetime\ndatetime.now()\n")
    assert _scan(fixture, is_clock_scoped=False) == []


def test_the_scan_flags_a_global_random_call(tmp_path: Path) -> None:
    fixture = tmp_path / "mod.py"
    fixture.write_text("import random\nrandom.random()\n")
    violations = _scan(fixture, is_clock_scoped=False)
    assert len(violations) == 1
    assert "random.random" in violations[0]


def test_the_scan_allows_an_explicit_random_instance(tmp_path: Path) -> None:
    fixture = tmp_path / "mod.py"
    fixture.write_text("import random\nrandom.Random(1).random()\n")
    # The outer call is `.random()` on an instance, not the `random` module
    # itself -- its attribute chain does not start with the bare name
    # "random", so it is not flagged. Only the `random.Random(1)`
    # construction is inspected, and "Random" is allow-listed.
    assert _scan(fixture, is_clock_scoped=False) == []


def test_the_scan_flags_numpy_random_seed(tmp_path: Path) -> None:
    fixture = tmp_path / "mod.py"
    fixture.write_text("import numpy as np\nnp.random.seed(1)\n")
    violations = _scan(fixture, is_clock_scoped=False)
    assert len(violations) == 1
    assert "random.seed" in violations[0]


def test_the_scan_allows_numpy_default_rng(tmp_path: Path) -> None:
    fixture = tmp_path / "mod.py"
    fixture.write_text("import numpy as np\nnp.random.default_rng(1)\n")
    assert _scan(fixture, is_clock_scoped=False) == []
