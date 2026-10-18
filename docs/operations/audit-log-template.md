# EVORYN Audit Log Template

This document defines fields for future audit events. It does not implement a logging backend.

## Required event fields

- event_id: unique immutable event identifier
- timestamp: ISO 8601 timestamp with timezone
- event_type: stable event category
- actor_type and actor_id: non-secret actor reference
- correlation_id: request / workflow correlation identifier
- resource_type and resource_id: sanitized resource reference
- action: attempted or completed action
- result: success / failure / denied / unknown
- reason_code: stable code for failures or policy decisions
- policy_version: applicable policy version, if any
- source: component or service name
- integrity_reference: optional hash / append-only storage reference

## Security rules

- Never log API keys, bearer tokens, passwords, private keys, full authorization headers, or exchange secrets.
- Sanitize untrusted external text and redact sensitive payload fields before logging.
- Do not treat logs as the sole source of truth for order state or reconciliation.
- Preserve event ordering and correlation where supported; document clock and retention assumptions.
- Restrict access to audit data and document retention / deletion policy before storing production events.

## Empty event template

```json
{
  "event_id": "<required>",
  "timestamp": "<ISO 8601 with timezone>",
  "event_type": "<required>",
  "actor_type": "<required>",
  "actor_id": "<non-secret reference>",
  "correlation_id": "<required>",
  "resource_type": "<required>",
  "resource_id": "<sanitized reference>",
  "action": "<required>",
  "result": "<success|failure|denied|unknown>",
  "reason_code": "<optional stable code>",
  "policy_version": "<optional version>",
  "source": "<component name>",
  "integrity_reference": "<optional>"
}
```
