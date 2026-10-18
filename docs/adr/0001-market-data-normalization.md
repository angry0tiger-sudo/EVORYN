# ADR-0001: Canonical Market Candle and Fail-Closed Quality Gates

- **Status:** Proposed
- **Date:** 2026-10-18
- **Owners:** EVORYN engineering owner
- **Related phases:** Phase 5, Market Data Infrastructure
- **Related changes:** `feat/market-data-foundation`

## Context

Market data must be normalized and evaluated for freshness, temporal coverage, ordering, duplicates, market identity, and candle interval before downstream consumers rely on it. Provider payloads and live APIs are outside the scope of this foundation step.

## Decision

- Represent normalized OHLCV candles using a typed immutable `MarketCandle` domain model and `Decimal` price/volume fields.
- Accept only an explicit schema containing `timestamp`, `open`, `high`, `low`, `close`, and `volume`. Provider-specific field aliases must be mapped explicitly at a future adapter boundary.
- Require timezone-aware timestamps and normalize parsed values to UTC.
- Reject malformed rows instead of fabricating values or silently dropping fields.
- Preserve input order during normalization. The quality gate reports duplicate timestamps, non-monotonic input, temporal gaps, stale/future timestamps, wrong market/interval, and optional coverage gaps.
- Reject the complete batch if any quality issue is present. Do not auto-sort, deduplicate, repair, or fill missing candles.
- Make no network calls and add no exchange-specific adapter.

## Alternatives considered

1. **Normalize permissively and drop malformed rows:** rejected because partial data can conceal gaps and distort downstream decisions.
2. **Sort and deduplicate during normalization:** rejected because it hides acquisition defects from observability and audit.
3. **Bind this step to a specific exchange schema:** deferred until the separate exchange-contract phase and official API review.

## Consequences

### Positive
- Deterministic, testable behavior independent of external services.
- Exact decimal representation and explicit timestamp semantics.
- Quality problems are visible to callers and can be logged or audited.

### Negative / trade-offs
- Provider payloads with different field names require explicit mapping.
- Strict batch rejection may reduce usable data until upstream acquisition defects are fixed.
- Coverage checks require callers to define expected candle-open timestamps explicitly.

## Security and safety impact

- No credentials, HTTP client, exchange adapter, order-writing method, or live execution path is introduced.
- Untrusted input values are validated before producing canonical candle records.
- Quality checks are informational gates; they do not execute trading decisions.

## Validation and evidence

- Unit tests cover decimal/UTC normalization, malformed OHLCV rows, empty/stale/future series, duplicates, out-of-order candles, time gaps, identity mismatch, and incomplete coverage.
- GitHub Actions CI must pass pytest, Ruff, mypy, Bandit, and pip-audit for the exact PR head.
- Independent review is required before merging.

## Rollback / supersession

Revert the PR commits or add a superseding ADR if downstream consumers require a different canonical candle schema or explicit partial-batch semantics.
