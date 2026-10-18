# Market Data Foundation

This phase implements local, deterministic processing only. It does not fetch data and contains no exchange adapter.

## Normalization

`evoryn.infrastructure.data.normalization.normalize_candle` accepts a mapping with explicit `timestamp`, `open`, `high`, `low`, `close`, and `volume` fields. Decimal-compatible values are converted to `Decimal`; timestamps must be timezone-aware and are converted to UTC. Invalid prices, volumes, OHLC relationships, missing fields, or ambiguous timestamps raise `ValueError`.

Batch normalization is all-or-error: a malformed row aborts the call. It preserves source order and does not deduplicate.

## Quality gate

`evaluate_market_data` accepts normalized candles, an aware `as_of` timestamp, and a `MarketDataQualityPolicy`. The report includes stable issue codes for:

- Empty datasets, stale data, and timestamps too far in the future.
- Duplicate or non-monotonic timestamps.
- Gaps between expected candle intervals.
- Wrong market identity or interval.
- Missing leading/trailing coverage when boundaries are supplied.

A report is accepted only when it contains no issues. The gate never sorts, repairs, fills, or silently removes data. Downstream consumers should treat a rejected report as not fit for use.

## Scope boundary

There is no HTTP client, URL, exchange credential, adapter implementation, persistence backend, strategy logic, or order execution here. Connecting a provider requires a separately reviewed adapter and official API contract evidence.
