# ADR 0009: Licensed External Market Data and News Evidence

- Status: Proposed for review
- Date: 2026-10-19
- Scope: Roadmap Phase 16
- Depends on: Phase 13 provenance/backtest, Phase 14 NLP/regime abstention and the source-of-truth
  rule that unsupported licenses or permissions fail closed

## Decision

1. Provide official HTTPS JSON connectors for CoinMarketCap quotes and CoinGecko market data.
   Their market data is contextual/research data, not a substitute for the canonical exchange
   execution price or an authority to place orders. CoinMarketCap uses its official API key header
   when a key is configured; keys are injected at construction and never placed in query strings.
2. Provide a generic `LicensedNewsJsonConnector` that can consume an explicitly configured and
   licensed provider JSON endpoint. It fetches JSON metadata only; a publisher's `article_url` is
   stored as metadata and never crawled or scraped. There is no TradingView scraper, browser
   automation or undocumented endpoint.
3. Require a reviewed `SourceLicenseAuthorization` for each source, including official HTTPS base
   URL, license/terms identifiers and revision, attribution, permitted uses, trust score, allowed
   languages, review expiry and content-retention rules. If the source authorization is absent,
   expired, mismatched or insufficient for metadata/research/NLP/storage, reject the data.
4. Normalize quotes/articles into immutable domain records with source/provider IDs, provider
   revision when available, publication/observation timestamp, retrieval timestamp, ingestion or
   analysis timestamp, language provenance, asset links, license/terms revision, attribution,
   payload/text SHA-256, and NLP model/data provenance.
5. Reject old or future-dated articles beyond policy, weak sources, missing publication time,
   mismatched declared content digests, active markup/scripts, hidden directional/control
   characters, duplicate identity/content and provider IDs that mutate content without an explicit
   revision. If a provider does not publish a signed or declared content digest, internal hashes
   detect post-ingestion mutations but do not prove the publisher's content was authentic.
6. Keep accepted news in a retention-aware in-memory corpus by default. It never silently persists
   unlimited raw article text. Storage/retention requires explicit authorization and expired content
   is purged. Production-scale encrypted durable retention/backups remain a separate reviewed step.
7. Add an optional local Hugging Face inference boundary. No model download, model loading or
   network inference occurs on import. The reviewed candidate is pinned to an immutable Git commit
   and must use reviewed label mapping, explicit language support, bounded text/token limits,
   verified source-text processing permission, and validated probability distribution/confidence.
8. Candidate weights declare Apache-2.0, but their model card identifies `takala/financial_phrasebank`
   as training data, whose dataset card declares CC BY-NC-SA 3.0. Since dataset/model rights for
   the intended use cannot be inferred from the weights license alone, the candidate's
   `training_data_rights_verified` default is false and analysis is refused until authorized review.
   Commercial use additionally requires a separate explicit approval flag.
9. Implement a conservative news overlay only for ablation: it can veto an existing long position
   when sufficiently trusted, sufficiently confident negative sentiment is available before a
   simulated candle opens. It cannot create/increase exposure, open shorts or alter risk limits.
10. Compare the exact same held-out test range for price-only and price-plus-news runs. Predeclare
    minimum event count and a joint result rule (net return no worse and maximum drawdown lower).
    Report `VALUE_ADDED_SUPPORTED`, `VALUE_ADDED_NOT_SUPPORTED`, or `INCONCLUSIVE`; an empty,
    stale or ineffective news corpus cannot be interpreted as a positive result.
11. A report includes dataset version, test indices, both run IDs, news-corpus hash, source and
    model licenses/revisions, parameters, event/veto counts, deltas and the explicit conclusion.
    Persist it with canonical JSON and integrity checks.
12. All outputs are informational only. The NLP adapter and external connectors do not receive
    risk/execution ports. No sentiment result may change a hard risk cap, clear Auto-Halt or issue
    any exchange order.

## Open-source and provider review

### CoinMarketCap API
- Official API reference: https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency
- Authentication reference: https://coinmarketcap.com/api/documentation/guides/authentication
- The connector injects the API key in the `X-CMC_PRO_API_KEY` header and restricts requests to
  the registered official API origin. Plan/credit and license rights must be verified for the
  selected account before enabling retrieval; no credentials or live request are part of tests.

### CoinGecko API
- Official API overview/licensing: https://www.coingecko.com/en/api
- Official licensing FAQ: https://support.coingecko.com/articles/1043089972-what-are-the-differences-between-commercial-and-custom-licenses
- Required plan/attribution/data-retention scope must be recorded in the authorization register.
  Rate limits and terms can change, so the registry review has an expiry and must be refreshed.

### News source boundary
No real news provider is globally enabled by default. The operator must register a named official
JSON endpoint, terms revision, allowed language and permitted purposes before use. A URL found on
the public web is not automatic permission to store or process its content.

### Hugging Face sentiment model
- Candidate weights: https://huggingface.co/mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis
- Immutable revision used in this implementation:
  `b986b74bfd4963613dcbc38d701747eda656ded4`
- Model-card license: Apache-2.0.
- Training data: https://huggingface.co/datasets/takala/financial_phrasebank at revision
  `8d3fe0c36d5feec6b3cc5e455b0fcb4820fb9964`; dataset card states CC BY-NC-SA 3.0 and lists
  non-commercial/share-alike conditions.
- Compatibility/cost: optional local Transformers/PyTorch runtime; model artifact is hundreds of
  megabytes, so loading cost is CPU/RAM/storage/download time, not per-call hosted-inference cost.
  Runtime dependencies are not made mandatory. Hosted inference providers are not invoked.
- Adoption decision: adapter only for isolated research; not enabled for any source by default.
  The training-data-rights flag is fail-closed until the exact intended use has been reviewed.
  Three-way sentiment is English-only in this reviewed candidate and does not provide validated
  crypto-specific market-impact prediction.

## Ablation definitions and evidence limits

- Price-only control and news-veto arm use the same immutable historical dataset version, costs,
  allocation assumptions, and held-out test indices.
- News availability is the maximum of publication, receipt and sentiment-analysis timestamps.
  A news event is usable only if all those steps completed strictly before the simulated bar open.
- The decision rule is predeclared and includes a minimum number of actual veto events, net-return
  non-inferiority and a strict maximum-drawdown improvement.
- Current unit fixture is synthetic and deliberately places a negative-news event before a
  synthetic price shock. A fixture that meets the rule shows that the code can detect that
  prescribed scenario; it is not evidence that news helps on real prices, that NLP labels are
  accurate, or that a future trading strategy will be more profitable.
- No real-market ablation result is claimed until authorized, versioned market/news data and the
  reviewed local model are run on an untouched holdout/walk-forward evaluation.

## Acceptance and rollback

Acceptance requires source authorization evidence, a reviewed model/data license, correct UTC
provenance, duplicate/quality tests, causal news timing, and an ablation report whose evidence and
limitations are preserved. If any source terms, model rights, content integrity, timing or licensing
assumptions are unresolved, disable that source/model and retain price-only behavior.
