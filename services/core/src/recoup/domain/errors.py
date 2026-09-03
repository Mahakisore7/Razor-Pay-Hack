"""Base exception for domain errors (ENGINEERING-STANDARDS SS2.4).

Domain errors subclass `RecoupError` and carry context -- `IllegalTransition
(case_id, from_state, to_state)`, not `ValueError("bad transition")` --
because a caught exception that cannot say which case or which states is a
caught exception someone will have to reproduce by hand.
"""

from __future__ import annotations

__all__ = ["RecoupError"]


class RecoupError(Exception):
    """Base class for all domain errors."""
