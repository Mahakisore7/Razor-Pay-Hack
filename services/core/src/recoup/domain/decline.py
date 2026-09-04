"""The canonical decline taxonomy (DOMAIN-MODEL SS2.3).

Razorpay, UPI, NACH, and card networks all return different failure strings
for the same underlying condition; this is the normalised vocabulary
detection, diagnosis, and policy operate on instead. The raw string is
preserved on the originating event for forensics -- normalising is lossy by
design, and the loss must stay recoverable.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["DeclineCategory", "RetryHorizon"]


class RetryHorizon(StrEnum):
    """How soon a retry might plausibly succeed, if at all."""

    MINUTES = "minutes"
    HOURS = "hours"
    DAYS = "days"  # e.g. aligned to the salary cycle
    NEXT_CYCLE = "next_cycle"  # e.g. a mandate's representation budget resets
    NOT_APPLICABLE = "not_applicable"  # retry cannot help; something must change first
    NEVER = "never"


class DeclineCategory(StrEnum):
    """The normalised failure taxonomy every downstream component reasons about.

    Each member carries three static properties that drive everything
    downstream. `retryable = False` means the executor refuses a retry
    action regardless of what the plan says -- the domain layer defending
    itself against a planning bug, since retrying e.g. an
    `INVALID_INSTRUMENT` cannot succeed and only burns mandate budget.
    """

    INSUFFICIENT_FUNDS = "insufficient_funds"
    ISSUER_DOWN = "issuer_down"
    ISSUER_DECLINED = "issuer_declined"
    NETWORK_TIMEOUT = "network_timeout"
    INVALID_INSTRUMENT = "invalid_instrument"
    EXPIRED_INSTRUMENT = "expired_instrument"
    MANDATE_REVOKED = "mandate_revoked"
    MANDATE_EXHAUSTED = "mandate_exhausted"
    MANDATE_AMOUNT_EXCEEDED = "mandate_amount_exceeded"
    LIMIT_EXCEEDED = "limit_exceeded"
    AUTHENTICATION_FAILED = "authentication_failed"
    RISK_BLOCKED = "risk_blocked"
    CUSTOMER_ABANDONED = "customer_abandoned"
    UNKNOWN = "unknown"

    @property
    def retryable(self) -> bool:
        return _PROPERTIES[self][0]

    @property
    def customer_action_required(self) -> bool:
        return _PROPERTIES[self][1]

    @property
    def retry_horizon(self) -> RetryHorizon:
        return _PROPERTIES[self][2]


# (retryable, customer_action_required, retry_horizon), per the DOMAIN-MODEL SS2.3
# table. That table leaves five cells unresolved -- a tri-state ("weakly" /
# "maybe" / "n/a") where these properties are booleans, or a member the table
# omits entirely. Resolved here, reasoned rather than left to KeyError the
# first time policy touches them:
#   - ISSUER_DECLINED: "weakly" retryable reads as True -- it IS permitted,
#     just planned cautiously; that caution is a planning-layer signal, not a
#     domain-layer gate. "maybe" customer_action_required defaults to False:
#     try the automated path first.
#   - CUSTOMER_ABANDONED: "n/a" retryable becomes False -- there is no prior
#     payment attempt to retry, only a funnel to re-enter via another channel.
#   - MANDATE_AMOUNT_EXCEEDED: same shape as an invalid/expired instrument --
#     the same amount cannot succeed against this mandate; needs a new
#     mandate or a smaller amount, not a blind retry.
#   - LIMIT_EXCEEDED: card/account limits are typically cycle-based and reset
#     without the customer doing anything, so this is modelled like
#     insufficient_funds rather than invalid_instrument.
#   - AUTHENTICATION_FAILED: only the customer can complete an OTP/3DS
#     challenge, so a system-initiated retry cannot succeed -- but a fresh
#     attempt is worth prompting soon, while intent is still warm.
#   - UNKNOWN: DOMAIN-MODEL is explicit that this is "not retryable, escalate
#     to human" -- read here as an ops escalation, not a customer-facing ask,
#     since we do not know what to ask the customer to do.
_PROPERTIES: dict[DeclineCategory, tuple[bool, bool, RetryHorizon]] = {
    DeclineCategory.INSUFFICIENT_FUNDS: (True, False, RetryHorizon.DAYS),
    DeclineCategory.ISSUER_DOWN: (True, False, RetryHorizon.HOURS),
    DeclineCategory.ISSUER_DECLINED: (True, False, RetryHorizon.DAYS),
    DeclineCategory.NETWORK_TIMEOUT: (True, False, RetryHorizon.MINUTES),
    DeclineCategory.INVALID_INSTRUMENT: (False, True, RetryHorizon.NOT_APPLICABLE),
    DeclineCategory.EXPIRED_INSTRUMENT: (False, True, RetryHorizon.NOT_APPLICABLE),
    DeclineCategory.MANDATE_REVOKED: (False, True, RetryHorizon.NOT_APPLICABLE),
    DeclineCategory.MANDATE_EXHAUSTED: (False, False, RetryHorizon.NEXT_CYCLE),
    DeclineCategory.MANDATE_AMOUNT_EXCEEDED: (False, True, RetryHorizon.NOT_APPLICABLE),
    DeclineCategory.LIMIT_EXCEEDED: (True, False, RetryHorizon.DAYS),
    DeclineCategory.AUTHENTICATION_FAILED: (False, True, RetryHorizon.HOURS),
    DeclineCategory.RISK_BLOCKED: (False, False, RetryHorizon.NEVER),
    DeclineCategory.CUSTOMER_ABANDONED: (False, True, RetryHorizon.HOURS),
    DeclineCategory.UNKNOWN: (False, False, RetryHorizon.NOT_APPLICABLE),
}
