# EVORYN Decision Ledger Template

This file is a schema/template only. Do not place credentials, account identifiers, or sensitive personal data in ledger entries.

Create one record per material decision. Prefer append-only records and preserve corrections as new linked records instead of silently rewriting history.

## Record fields

- Decision ID: unique immutable identifier
- Recorded at: ISO 8601 timestamp with timezone
- Actor: component or approved human role
- Context reference: sanitized input / dataset / snapshot identifier
- Market reference: canonical symbol and market type, if applicable
- Proposal reference: signal or intent identifier, if applicable
- Risk decision: ALLOW / REDUCE / REJECT / HALT, or NOT_APPLICABLE
- Rationale: concise structured explanation
- Policy version: version/hash of applicable deterministic policy
- Data quality: source, freshness, completeness, quality flags
- Outcome reference: order/fill/reconciliation reference, if applicable
- Correlation ID: trace identifier shared across related events
- Integrity reference: hash or immutable storage reference, when implemented
- Review status: pending / reviewed / rejected
- Reviewer reference: approved role or review record

## Empty record template

| Field | Value |
|---|---|
| Decision ID | <required> |
| Recorded at | <ISO 8601 with timezone> |
| Actor | <component or role> |
| Context reference | <sanitized reference> |
| Proposal reference | <optional reference> |
| Risk decision | <enum> |
| Rationale | <required> |
| Policy version | <version/hash> |
| Data quality | <source/freshness/flags> |
| Outcome reference | <optional reference> |
| Correlation ID | <required> |
| Integrity reference | <optional until implemented> |
| Review status | <status> |
