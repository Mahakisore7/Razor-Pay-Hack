"""human_review -- stubbed, cost-accounted (T2.7). See `_stub.py`. Unlike
the messaging channels, a real implementation here is a console queue
entry (PHASE-06), not a provider call -- still nothing this executor
calls directly.
"""

from __future__ import annotations

from recoup.execution.channels._stub import stub_handle as handle

__all__ = ["handle"]
