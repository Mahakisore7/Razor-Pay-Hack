"""SQLAlchemy 2.0 models for every table in DATA-MODEL.md.

Four tables encode a domain invariant directly in the schema (DATA-MODEL
SS3): `audit_events` (append-only, hash-chained, trigger-enforced),
`cases` (`cost_within_ceiling`, `resolved_iff_terminal`, the partial-unique
`cases_open_dedup` index), `scheduled_actions` (the durable outbox), and
`consent_events` (an append-only ledger, never a mutable boolean column).

Enums are `text` + `CHECK`, not a Postgres native enum (DATA-MODEL SS1):
adding a value to a PG enum takes a lock; a CHECK constraint is a cheap
swap. The CHECK constraints this file declares, the partial indexes, and
the audit-immutability trigger are what Alembic's autogenerate misses --
the initial migration is hand-reviewed for exactly that reason
(DATA-MODEL SS6).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import ClassVar

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# No "ck" entry: every CheckConstraint below is given a full, explicit name
# (several -- cost_within_ceiling, resolved_iff_terminal, audit_seq_positive
# -- are DATA-MODEL's own bespoke names, not a `ck_<table>_<column>` shape).
# A "ck" convention combines with an explicit name rather than replacing it,
# which double-prefixed every check constraint here until this was reviewed
# by hand and removed.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # DATA-MODEL SS1: timestamps are `timestamptz`, always UTC. Plain
    # `datetime` maps to a naive `TIMESTAMP` by default; every `Mapped[
    # datetime]` column needs `timezone=True` for this to be true, so it is
    # set once here rather than repeated (and inevitably missed somewhere)
    # on every column.
    type_annotation_map: ClassVar[dict[type, DateTime]] = {datetime: DateTime(timezone=True)}


class _CreatedAtMixin:
    created_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


class _UpdatedAtMixin(_CreatedAtMixin):
    # Kept current by a `set_updated_at()` trigger (the migration), not the
    # application -- a row updated through any path gets a correct
    # timestamp, not just the ones application code remembers to touch.
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))


# --- PII isolation (DATA-MODEL SS4) ------------------------------------------


class Customer(Base, _CreatedAtMixin):
    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    razorpay_customer_id: Mapped[str | None] = mapped_column(unique=True)
    contact_hash: Mapped[str]
    timezone: Mapped[str] = mapped_column(server_default=text("'Asia/Kolkata'"))


class CustomerPII(Base):
    __tablename__ = "customer_pii"

    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="RESTRICT"), primary_key=True
    )
    name_enc: Mapped[bytes | None]
    phone_enc: Mapped[bytes | None]
    email_enc: Mapped[bytes | None]
    key_version: Mapped[int]


# --- Consent (DATA-MODEL SS3.4) ----------------------------------------------


class ConsentEventRow(Base, _CreatedAtMixin):
    """No `customers.sms_consent` boolean exists anywhere in this schema,
    deliberately: consent is folded from this append-only ledger at a point
    in time (`recoup.domain.consent.consent_at`), because a mutable boolean
    cannot answer "were they opted in when contacted" after the fact."""

    __tablename__ = "consent_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="RESTRICT")
    )
    channel: Mapped[str]
    granted: Mapped[bool]
    source: Mapped[str]
    occurred_at: Mapped[datetime]

    __table_args__ = (
        CheckConstraint(
            "source IN ('checkout','sms_stop','dashboard','dnd_sync','import')",
            name="ck_consent_events_source",
        ),
        Index("consent_lookup", "customer_id", "channel", text("occurred_at DESC")),
    )


class ContactEvent(Base, _CreatedAtMixin):
    """Every outbound contact, independent of consent -- what a frequency
    cap counts against. Minimal: the code that writes and reads this table
    (execution's channel adapters, the policy engine's frequency-cap rule)
    is Phase 2+ work; this is the schema it will need."""

    __tablename__ = "contact_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="RESTRICT")
    )
    channel: Mapped[str]
    occurred_at: Mapped[datetime]

    __table_args__ = (Index("contact_lookup", "customer_id", "channel", text("occurred_at DESC")),)


# --- Mandates (DOMAIN-MODEL SS11) --------------------------------------------


class MandateRow(Base, _UpdatedAtMixin):
    __tablename__ = "mandates"

    id: Mapped[str] = mapped_column(primary_key=True)  # Razorpay mandate id
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="RESTRICT")
    )
    rail: Mapped[str]
    max_amount_paise: Mapped[int]
    frequency: Mapped[str]
    valid_from: Mapped[date]
    valid_until: Mapped[date]
    status: Mapped[str]
    representations_used_this_cycle: Mapped[int] = mapped_column(server_default=text("0"))
    representation_cap: Mapped[int]

    __table_args__ = (
        CheckConstraint(
            "rail IN ('upi_autopay','enach','emandate','card')", name="ck_mandates_rail"
        ),
        CheckConstraint(
            "status IN ('pending','active','paused','revoked','expired')",
            name="ck_mandates_status",
        ),
        CheckConstraint("max_amount_paise > 0", name="ck_mandates_max_amount_positive"),
        CheckConstraint(
            "representations_used_this_cycle >= 0", name="ck_mandates_representations_non_negative"
        ),
        CheckConstraint(
            "representations_used_this_cycle <= representation_cap",
            name="ck_mandates_representations_within_cap",
        ),
        CheckConstraint("valid_from <= valid_until", name="ck_mandates_valid_range"),
    )


# --- Detection input (raw ingestion -- Phase 2+ writes this) -----------------


class RawEvent(Base, _CreatedAtMixin):
    """The unprocessed webhook/API payload a signal was detected from.
    Retained 90 days (DATA-MODEL SS7) to allow detector re-runs; the
    detectors that populate this are Phase 2 (L1-L3) work.

    `provider_event_id` is what makes a redelivery a no-op (TR-3). Razorpay's
    webhook body carries no delivery-level id of its own (only resource ids,
    which repeat across an object's lifecycle) -- so ingestion derives it as
    `sha256(raw_body)`, which is stable across a redelivery of the same
    bytes and never collides across genuinely different events."""

    __tablename__ = "raw_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    source: Mapped[str]  # e.g. "razorpay_webhook"
    event_type: Mapped[str]  # e.g. "payment.failed"
    provider_event_id: Mapped[str]
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    decline_category: Mapped[str | None]
    received_at: Mapped[datetime]

    __table_args__ = (
        UniqueConstraint("provider_event_id", name="uq_raw_events_provider_event_id"),
    )


# --- Signals and cases (DOMAIN-MODEL SS3-4) ----------------------------------


class SignalRow(Base, _CreatedAtMixin):
    __tablename__ = "signals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    leak_class: Mapped[str]
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="RESTRICT")
    )
    at_risk_paise: Mapped[int]
    detected_at: Mapped[datetime]
    source_event_ids: Mapped[list[str]] = mapped_column(JSONB)
    decline_category: Mapped[str | None]
    context: Mapped[dict[str, object]] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint(
            "leak_class IN ('L1','L2','L3','L4','L5','L6')", name="ck_signals_leak_class"
        ),
        CheckConstraint("at_risk_paise > 0", name="ck_signals_at_risk_positive"),
    )


_TERMINAL_STATES_SQL = "'recovered','partially_recovered','lost','expired','suppressed'"


class CaseRow(Base, _UpdatedAtMixin):
    __tablename__ = "cases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    signal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("signals.id", ondelete="RESTRICT")
    )
    customer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="RESTRICT")
    )
    state: Mapped[str]
    arm: Mapped[str]
    at_risk_paise: Mapped[int]
    cost_spent_paise: Mapped[int] = mapped_column(server_default=text("0"))
    cost_ceiling_paise: Mapped[int]
    playbook_id: Mapped[str | None]
    playbook_version: Mapped[int | None]
    bench_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("bench_runs.id", ondelete="RESTRICT")
    )
    opened_at: Mapped[datetime]
    resolved_at: Mapped[datetime | None]

    __table_args__ = (
        CheckConstraint("arm IN ('control','baseline','treatment')", name="ck_cases_arm"),
        CheckConstraint("at_risk_paise > 0", name="ck_cases_at_risk_positive"),
        CheckConstraint("cost_spent_paise >= 0", name="ck_cases_cost_spent_non_negative"),
        CheckConstraint("cost_ceiling_paise >= 0", name="ck_cases_cost_ceiling_non_negative"),
        # I2: the cost guardrail is meaningless if it can be breached --
        # this is defence in depth behind the domain-layer check in
        # Case.record_cost, catching a concurrent double-spend at commit.
        CheckConstraint("cost_spent_paise <= cost_ceiling_paise", name="cost_within_ceiling"),
        # "Terminal state without a resolution timestamp" is unrepresentable
        # rather than merely tested for.
        CheckConstraint(
            f"(state IN ({_TERMINAL_STATES_SQL}) AND resolved_at IS NOT NULL) "
            f"OR (state NOT IN ({_TERMINAL_STATES_SQL}) AND resolved_at IS NULL)",
            name="resolved_iff_terminal",
        ),
        # Partial unique index: one *open* case per (customer, amount) --
        # a customer may have many historical cases at the same amount, but
        # never two open ones. Declared with Index rather than
        # UniqueConstraint because a partial index needs a WHERE clause.
        Index(
            "cases_open_dedup",
            "customer_id",
            "at_risk_paise",
            unique=True,
            postgresql_where=text(f"state NOT IN ({_TERMINAL_STATES_SQL})"),
        ),
    )


# --- Diagnosis (DOMAIN-MODEL SS5) --------------------------------------------


class DiagnosisRow(Base, _CreatedAtMixin):
    __tablename__ = "diagnoses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cases.id", ondelete="RESTRICT"), unique=True
    )
    method: Mapped[str]
    computed_at: Mapped[datetime]
    llm_model: Mapped[str | None]
    fallback_reason: Mapped[str | None]

    __table_args__ = (
        CheckConstraint(
            "method IN ('statistical','llm_ranked','abstained')", name="ck_diagnoses_method"
        ),
    )


class HypothesisRow(Base, _CreatedAtMixin):
    __tablename__ = "hypotheses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    diagnosis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("diagnoses.id", ondelete="RESTRICT")
    )
    rank: Mapped[int]  # 0 = highest confidence; DOMAIN-MODEL's tuple order, made explicit
    root_cause: Mapped[str]
    confidence: Mapped[float]
    narration: Mapped[str | None]

    __table_args__ = (
        UniqueConstraint("diagnosis_id", "rank", name="uq_hypotheses_diagnosis_rank"),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_hypotheses_confidence_range"
        ),
    )


class EvidenceRow(Base, _CreatedAtMixin):
    __tablename__ = "evidence"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    hypothesis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("hypotheses.id", ondelete="RESTRICT")
    )
    slice_dimension: Mapped[str]
    slice_value: Mapped[str]
    failure_rate: Mapped[float]
    baseline_rate: Mapped[float]
    sample_size: Mapped[int]
    z_statistic: Mapped[float]
    p_value: Mapped[float]


# --- Plans and actions (DOMAIN-MODEL SS6-7) ----------------------------------


class PlanRow(Base, _CreatedAtMixin):
    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cases.id", ondelete="RESTRICT"), unique=True
    )
    playbook_id: Mapped[str]
    playbook_version: Mapped[int]
    total_expected_cost_paise: Mapped[int]

    __table_args__ = (
        CheckConstraint(
            "total_expected_cost_paise >= 0", name="ck_plans_total_expected_cost_non_negative"
        ),
    )


class PlannedStepRow(Base, _CreatedAtMixin):
    __tablename__ = "planned_steps"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plans.id", ondelete="RESTRICT")
    )
    step_id: Mapped[str]
    due_at: Mapped[datetime]
    expected_cost_paise: Mapped[int]

    __table_args__ = (UniqueConstraint("plan_id", "step_id", name="uq_planned_steps_plan_step"),)


class ActionRow(Base, _CreatedAtMixin):
    __tablename__ = "actions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cases.id", ondelete="RESTRICT")
    )
    step_id: Mapped[str]
    attempt: Mapped[int]
    channel: Mapped[str]
    idempotency_key: Mapped[str] = mapped_column(unique=True)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    cost_paise: Mapped[int]
    due_at: Mapped[datetime]

    __table_args__ = (
        CheckConstraint("attempt >= 1", name="ck_actions_attempt_positive"),
        UniqueConstraint("case_id", "step_id", "attempt", name="uq_actions_case_step_attempt"),
    )


class ScheduledActionRow(Base, _CreatedAtMixin):
    """The durable outbox (DATA-MODEL SS3.3). Claimed with
    `FOR UPDATE SKIP LOCKED` so many workers can claim disjoint batches
    without blocking each other; `claim_expires_at` makes a worker crash
    recoverable, and `actions.idempotency_key` makes the reclaim safe."""

    __tablename__ = "scheduled_actions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    action_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actions.id", ondelete="RESTRICT")
    )
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cases.id", ondelete="RESTRICT")
    )
    due_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(server_default=text("'pending'"))
    claimed_by: Mapped[str | None]
    claimed_at: Mapped[datetime | None]
    claim_expires_at: Mapped[datetime | None]
    attempts: Mapped[int] = mapped_column(server_default=text("0"))
    last_error: Mapped[str | None]

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','claimed','done','failed','cancelled')",
            name="ck_scheduled_actions_status",
        ),
        Index("scheduled_due", "due_at", postgresql_where=text("status = 'pending'")),
        Index(
            "scheduled_expired_claims",
            "claim_expires_at",
            postgresql_where=text("status = 'claimed'"),
        ),
    )


class PolicyDecisionRow(Base, _CreatedAtMixin):
    __tablename__ = "policy_decisions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    action_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("actions.id", ondelete="RESTRICT")
    )
    attempt: Mapped[int]
    verdict: Mapped[str]
    rule_id: Mapped[str | None]
    inputs: Mapped[dict[str, object]] = mapped_column(JSONB)
    defer_until: Mapped[datetime | None]
    decided_at: Mapped[datetime]

    __table_args__ = (
        CheckConstraint("verdict IN ('allow','deny','defer')", name="ck_policy_decisions_verdict"),
        UniqueConstraint("action_id", "attempt", name="uq_policy_decisions_action_attempt"),
    )


class OutcomeRow(Base, _CreatedAtMixin):
    __tablename__ = "outcomes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cases.id", ondelete="RESTRICT"), unique=True
    )
    kind: Mapped[str]
    recovered_paise: Mapped[int]
    attributed_payment_id: Mapped[str | None]
    attributed_step_id: Mapped[str | None]
    reason_code: Mapped[str | None]
    resolved_at: Mapped[datetime]

    __table_args__ = (
        CheckConstraint(
            "kind IN ('recovered','partially_recovered','lost','expired','suppressed','escalated')",
            name="ck_outcomes_kind",
        ),
        CheckConstraint("recovered_paise >= 0", name="ck_outcomes_recovered_non_negative"),
        # Outcome's own constructor already refuses this (DOMAIN-MODEL SS9);
        # repeated here as defence in depth for any row written outside it.
        CheckConstraint(
            "kind IN ('recovered','partially_recovered') OR reason_code IS NOT NULL",
            name="ck_outcomes_reason_code_required",
        ),
    )


class Payment(Base, _CreatedAtMixin):
    """A Razorpay payment, optionally attributed to a case. Minimal: the
    attribution logic that populates `case_id` is Phase 2+ work."""

    __tablename__ = "payments"

    id: Mapped[str] = mapped_column(primary_key=True)  # Razorpay payment id, e.g. "pay_..."
    case_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cases.id", ondelete="RESTRICT")
    )
    amount_paise: Mapped[int]
    method: Mapped[str | None]
    captured_at: Mapped[datetime]

    __table_args__ = (CheckConstraint("amount_paise > 0", name="ck_payments_amount_positive"),)


class BenchRun(Base, _UpdatedAtMixin):
    """One run of the three-arm benchmark (ROADMAP P3). Retained
    indefinitely (DATA-MODEL SS7) for reproducibility of published results
    -- `seed` is what a reader would need to reproduce it."""

    __tablename__ = "bench_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    seed: Mapped[int]
    config: Mapped[dict[str, object]] = mapped_column(JSONB)
    started_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]


# --- Audit (DOMAIN-MODEL SS10) -----------------------------------------------


class AuditEventRow(Base):
    """No `_CreatedAtMixin`: `occurred_at` is already the authoritative
    timestamp for an append-only row, and immutability is enforced by the
    `audit_no_update`/`audit_no_delete` triggers in the migration, not by
    this model."""

    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    case_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("cases.id", ondelete="RESTRICT")
    )
    seq: Mapped[int]
    kind: Mapped[str]
    payload: Mapped[dict[str, object]] = mapped_column(JSONB)
    actor_type: Mapped[str]
    actor_id: Mapped[str | None]
    trace_id: Mapped[str]
    occurred_at: Mapped[datetime]
    prev_hash: Mapped[str]
    hash: Mapped[str]

    __table_args__ = (
        CheckConstraint(
            "actor_type IN ('system','user','scheduler')", name="ck_audit_events_actor_type"
        ),
        UniqueConstraint("case_id", "seq", name="audit_seq_unique"),
        CheckConstraint("seq >= 1", name="audit_seq_positive"),
        Index("audit_case_seq", "case_id", "seq"),
        Index("audit_kind_time", "kind", text("occurred_at DESC")),
    )
