"""The Razorpay `error_reason` -> DeclineCategory loader (RAZORPAY-INTEGRATION SS5).

An unmapped or absent reason must categorise as UNKNOWN -- conservative by
default, since an unmapped failure is a gap in our knowledge, not a licence
to guess.
"""

import pytest

from recoup.domain.decline import DeclineCategory
from recoup.gateway.decline_taxonomy import categorize, parse_decline_taxonomy


def test_categorize_maps_a_known_reason() -> None:
    assert categorize("card_expired") == DeclineCategory.EXPIRED_INSTRUMENT


def test_categorize_unmapped_reason_is_unknown() -> None:
    assert categorize("some_reason_nobody_has_seen") == DeclineCategory.UNKNOWN


def test_categorize_none_is_unknown() -> None:
    assert categorize(None) == DeclineCategory.UNKNOWN


def test_bundled_taxonomy_loads_and_validates() -> None:
    from recoup.gateway.decline_taxonomy import load_decline_taxonomy

    mapping = load_decline_taxonomy()
    assert mapping["payment_declined_by_issuer"] == DeclineCategory.ISSUER_DECLINED
    assert all(isinstance(category, DeclineCategory) for category in mapping.values())


def test_parse_rejects_missing_mappings_key() -> None:
    with pytest.raises(ValueError, match="mappings"):
        parse_decline_taxonomy("not_mappings: {}")


def test_parse_rejects_non_dict_document() -> None:
    with pytest.raises(ValueError, match="mappings"):
        parse_decline_taxonomy("- just\n- a\n- list\n")


def test_parse_rejects_unknown_category() -> None:
    with pytest.raises(ValueError, match="unknown category"):
        parse_decline_taxonomy("mappings:\n  some_reason: NOT_A_REAL_CATEGORY\n")


def test_parse_rejects_non_string_entry() -> None:
    with pytest.raises(ValueError, match="invalid mapping entry"):
        parse_decline_taxonomy("mappings:\n  some_reason: 42\n")


def test_parse_rejects_duplicate_reason() -> None:
    # PyYAML's default loader would silently keep the second value; the
    # taxonomy's own _UniqueKeyLoader is what turns this into an error.
    with pytest.raises(ValueError, match="duplicate"):
        parse_decline_taxonomy(
            "mappings:\n  card_expired: EXPIRED_INSTRUMENT\n  card_expired: INVALID_INSTRUMENT\n"
        )


def test_parse_accepts_a_minimal_valid_document() -> None:
    mapping = parse_decline_taxonomy("mappings:\n  foo_bar: UNKNOWN\n")
    assert mapping == {"foo_bar": DeclineCategory.UNKNOWN}
