# ADR-0007 — Money as integer paise

- **Status:** Accepted
- **Date:** 2026-09-02

## Context

Recoup sums thousands of at-risk amounts, recovered amounts, and per-action costs,
then reports the totals as its headline result. Money representation is therefore
not a stylistic choice — it determines whether the headline number is defensible.

## Decision

A `Money` value object holding **integer paise**. No float constructor exists;
`Money(2499.99)` raises `TypeError`. Money crosses API boundaries as
`{"paise": 249999, "currency": "INR"}`, never as a decimal string or float.
Division is an explicit `allocate()` that guarantees the parts sum to the whole.

## Rationale

`0.1 + 0.2 == 0.30000000000000004`. That is the familiar reason, and it is
sufficient on its own.

The deeper reason is accumulation. A single float rounding error is invisible.
Summed across 2,000 cases and three arms, it produces a headline number that
differs between runs — which breaks the reproducibility requirement (TR-37) and,
worse, makes every reported figure unverifiable. A reviewer who recomputes a total
and gets a different answer has no reason to trust anything else in the report.

Removing the float *constructor* rather than merely discouraging floats is the
important part. A convention that floats should not be used is a convention that
will be violated once, silently, by whoever is tired. A `TypeError` is not.

`allocate()` exists because splitting money is where naive rounding loses paise:
₹1.00 split three ways is 34+33+33, not 33.33 three times. Deterministic remainder
distribution means the parts always sum to the original.

## Alternatives rejected

**`Decimal`.** Correct arithmetic, and a reasonable choice. Rejected because it
still permits construction from a float (`Decimal(0.1)` silently inherits the
binary error), and because integer columns make an accidental float insert a
database type error rather than a silent truncation.

**Float with rounding at boundaries.** The error accumulates before the rounding.

**Integer paise as a bare `int`.** No currency safety, no protection against
adding paise to rupees, and no allocation guarantee. The wrapper costs almost
nothing and makes a class of bug unrepresentable.

## Consequences

**Positive.** Exact arithmetic. Reproducible totals. Currency mixing is a type
error. Allocation always sums correctly. A float entering the money path fails
loudly and immediately.

**Negative.** Slightly more verbose at every call site. Serialisation is less
human-friendly — `{"paise": 249999}` requires a reader to know the convention.

**Accepted.** The verbosity is paid once per call site; the correctness is
collected on every sum in every report.
