## What
One paragraph: what this changes.

## Why
The problem being solved. Link the requirement (FR-n / TR-n) or issue.

## How
Notable implementation decisions. Anything a reviewer would otherwise
have to reverse-engineer from the diff.

## Testing
- [ ] Unit tests added/updated
- [ ] Property tests updated (if policy/attribution/domain)
- [ ] Integration tests pass
- [ ] `make test-no-llm` passes (if diagnosis touched)
- [ ] Benchmark re-run (if it could move the numbers)

## Risk
What could break. What is not covered.

## Checklist
- [ ] CI green
- [ ] Types clean (mypy strict / tsc strict)
- [ ] No secrets, no PII in logs
- [ ] Docs updated if behaviour changed
- [ ] ADR added if an architectural decision was made

Closes #
