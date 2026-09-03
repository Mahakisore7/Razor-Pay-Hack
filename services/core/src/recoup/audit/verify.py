"""The audit chain verifier (DOMAIN-MODEL SS10): `recoup audit verify --case
<id>` recomputes the chain and reports the first divergence."""

from __future__ import annotations

from collections.abc import Sequence

from recoup.audit.events import AuditEvent, compute_hash

__all__ = ["verify_chain"]


def verify_chain(events: Sequence[AuditEvent]) -> int | None:
    """Recompute one case's chain in the given order. Returns the first
    `seq` position where it diverges, or `None` if the whole chain is intact.

    A single combined check per position catches all three ways a chain can
    break: a tampered payload (recomputed hash disagrees), a reordering
    (the event at this position isn't the one with this seq), and a deleted
    event (the seq/prev_hash link this position expected is missing).
    """
    expected_prev_hash = ""
    for expected_seq, event in enumerate(events, start=1):
        if (
            event.seq != expected_seq
            or event.prev_hash != expected_prev_hash
            or event.hash != compute_hash(event)
        ):
            return expected_seq
        expected_prev_hash = event.hash
    return None
