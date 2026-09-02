"""The module structure is architecture, so it is asserted rather than assumed.

If a package is renamed or removed, the import-linter contracts that constrain
it would silently stop constraining anything. This test makes that loud.
"""

import importlib

import pytest

PIPELINE_MODULES = [
    "recoup.domain",
    "recoup.policy",
    "recoup.detection",
    "recoup.diagnosis",
    "recoup.planning",
    "recoup.execution",
    "recoup.attribution",
    "recoup.gateway",
    "recoup.audit",
    "recoup.bench",
    "recoup.api",
    "recoup.platform",
]


@pytest.mark.parametrize("module_name", PIPELINE_MODULES)
def test_pipeline_module_is_importable(module_name: str) -> None:
    assert importlib.import_module(module_name) is not None


@pytest.mark.parametrize("module_name", PIPELINE_MODULES)
def test_pipeline_module_documents_its_responsibility(module_name: str) -> None:
    """A module with no docstring is a module whose boundary nobody defined."""
    module = importlib.import_module(module_name)
    assert module.__doc__, f"{module_name} has no docstring"
