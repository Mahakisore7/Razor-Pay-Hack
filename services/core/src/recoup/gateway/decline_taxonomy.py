"""Normalises Razorpay's raw `error_reason` strings into the canonical
DeclineCategory taxonomy (RAZORPAY-INTEGRATION.md SS5).

Razorpay, UPI, NACH, and card networks each return different failure
strings for the same underlying condition; this is the one place that
vocabulary gets translated, from a versioned, reviewable YAML file rather
than a hardcoded if/elif chain that grows unauditable over time.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
from typing import Any  # yaml's Node types are stubbed loosely; see _UniqueKeyLoader below

import yaml

from recoup.domain.decline import DeclineCategory

__all__ = ["categorize", "load_decline_taxonomy", "parse_decline_taxonomy"]


class _UniqueKeyLoader(yaml.SafeLoader):
    """A SafeLoader that rejects duplicate mapping keys.

    PyYAML's default loader silently keeps the last value on a duplicate
    key -- exactly the mistake a hand-edited taxonomy file would make
    silently, and exactly the mistake a loader should refuse rather than
    guess about. Still a SafeLoader underneath, so no constructor gains the
    ability to execute arbitrary Python (S506's concern with `yaml.load`).
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise ValueError(f"duplicate key in YAML mapping: {key!r}")
            seen.add(key)
        return super().construct_mapping(node, deep)


def parse_decline_taxonomy(raw_yaml: str) -> dict[str, DeclineCategory]:
    """Parse and validate a decline-taxonomy YAML document.

    Separated from `load_decline_taxonomy` so the validation rules -- an
    unknown category name, a duplicate reason, a malformed document -- are
    each directly testable without touching the filesystem.
    """
    parsed = yaml.load(raw_yaml, Loader=_UniqueKeyLoader)  # noqa: S506 -- _UniqueKeyLoader is a SafeLoader
    if not isinstance(parsed, dict) or not isinstance(parsed.get("mappings"), dict):
        raise ValueError("decline taxonomy YAML must have a top-level 'mappings' mapping")

    valid_names = {member.name: member for member in DeclineCategory}
    mapping: dict[str, DeclineCategory] = {}
    for reason, category_name in parsed["mappings"].items():
        if not isinstance(reason, str) or not isinstance(category_name, str):
            raise ValueError(f"invalid mapping entry: {reason!r} -> {category_name!r}")
        if category_name not in valid_names:
            raise ValueError(
                f"decline taxonomy maps {reason!r} to unknown category {category_name!r}"
            )
        mapping[reason] = valid_names[category_name]

    return mapping


@lru_cache
def load_decline_taxonomy() -> dict[str, DeclineCategory]:
    """Parse and validate the bundled mapping. Cached: read once, not per call."""
    raw = resources.files("recoup.gateway").joinpath("decline_taxonomy.yaml").read_text("utf-8")
    return parse_decline_taxonomy(raw)


def categorize(razorpay_reason: str | None) -> DeclineCategory:
    """Map a raw Razorpay `error_reason` to its canonical category.

    An unmapped or absent reason is `UNKNOWN`, deliberately: a decline code
    we have not mapped is a gap in our knowledge, not a licence to guess.
    """
    if razorpay_reason is None:
        return DeclineCategory.UNKNOWN
    return load_decline_taxonomy().get(razorpay_reason, DeclineCategory.UNKNOWN)
