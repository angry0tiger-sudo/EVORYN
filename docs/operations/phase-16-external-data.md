# Phase 16: External Market Data, Licensed News and NLP

## Scope

Phase 16 adds official JSON connectors, reviewed source authorizations, a news quality gate,
retention-aware in-memory corpus, optional local Hugging Face sentiment inference and a paired
price-only/news-ablation report. It does not add exchange order logic, a live news source, a
TradingView scraper, or an automatic financial decision path.

## CoinMarketCap and CoinGecko

- Use `CoinMarketCapConnector` with a numeric provider asset ID. Inject the secret through a
  secret store and an HTTP header; do not save keys in configuration, URLs, query strings or logs.
- Use `CoinGeckoConnector` with the exact canonical provider slug, not an ambiguous ticker symbol.
- Both connectors require the corresponding source ID to be present in `SourceLicenseRegistry`
  with an active authorization for `DataUse.MARKET_REFERENCE` and matching official API base.
- The connector records provider-observed time, retrieval time, provider ID, quote currency, source
  license/terms revision, attribution and payload SHA-256.
- No HTTP request is made by tests. Live access remains off until the operator reviews current plan,
  allowed usage, attribution, retention, account limits and costs for the intended purpose.

## Licensed news JSON intake

Configure `LicensedNewsJsonConnector` with a relative endpoint path and explicit `NewsFieldMapping`.
The registered source must authorize `DataUse.NEWS_METADATA`; storage and NLP each require their own
permission. Only provider-returned title/summary metadata is processed. The stored publisher URL is
not fetched. There is no crawler or scraping logic for TradingView or publisher pages.

Every candidate preserves provider/source IDs, optional source revision, publication time, retrieval
time, language plus evidence for that language, source name, related asset IDs, provider-payload hash,
and source license/terms revision and attribution. If the provider has no article ID, the connector
uses a deterministic URL hash and labels that fact rather than pretending the ID came from the source.

## Quality, duplication and manipulation limits

`NewsQualityGate` rejects unauthorized or expired source use, weak trust scores, unsupported
languages, missing/old/future publication timestamps, stale receipts, malformed/oversized content,
control/bidi overrides, active HTML/script markers, declared digest mismatches, duplicate content
and provider IDs that change content without an explicit revision.

Internal SHA-256 values detect changes after receipt and allow exact dedup/reproducibility. They do
not independently prove that a publisher's original content was truthful or untampered; that would
require an authentic provider signature or trusted source digest. Records with such stronger evidence
retain the evidence type. No content is fetched from the publisher URL to “verify” it.

## Source authorization and retention

Maintain an explicit source license registry: source ID, official base URL, license ID/URI, terms
URI/revision, reviewer, acceptance and expiry timestamps, permitted data uses, attribution, source
trust score, allowed languages and raw-content retention duration. Missing permission fails closed.
`NewsEventCorpus` requires explicit storage and analysis permission and purges records at retention
expiry. The included corpus is in-memory. Durable encrypted storage, backup and retention audit need
separate review before deployment.

## Hugging Face sentiment adapter

The candidate is:
`mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis` pinned to
`b986b74bfd4963613dcbc38d701747eda656ded4`. The model card lists Apache-2.0, but the referenced
`takala/financial_phrasebank` training dataset is CC BY-NC-SA 3.0. The implementation therefore
defaults `training_data_rights_verified=False`; inference refuses to proceed without explicit
rights review. Commercial use additionally requires `commercial_use_approved=True`.

No weights are downloaded and no transformer is loaded on import. Install the optional NLP runtime
only inside an approved isolated environment. Only English is declared supported; unsupported
language, expired model review, source permission mismatch, content-integrity failure, low confidence
or malformed probability output yields an abstention outcome. The three-way sentiment is contextual
metadata, not a market-impact classifier.

## News ablation

`evaluate_news_ablation` executes the price-only strategy and its conservative news-veto variant on
the exact same Phase 13 test indices and cost assumptions. The news overlay only suppresses existing
long exposure for sufficiently negative, confidence-qualified, trusted news that has been published,
retrieved and analyzed **strictly before** the simulated candle open. News arriving at or after that
open cannot affect that decision.

The policy predeclares the minimum number of veto events and returns one of:
- `value_added_supported`: event count is adequate, net return does not decline, and maximum drawdown
  is strictly lower on the supplied holdout.
- `value_added_not_supported`: events are adequate but the joint performance criterion is not met.
- `inconclusive`: insufficient effective events, no matching evidence, or no eligible news.

These are metrics for that single dataset/test window only, not a general statistical proof.
The current unit fixture synthesizes a negative-news event before a price crash to exercise the
expected path. It does **not** establish real-market improvement. Only use an authorized,
versioned dataset and an untouched holdout/walk-forward experiment for a performance claim.

## Trading and risk boundary

News sentiment cannot create a buy signal, increase target exposure, short, change hard risk caps,
clear Auto-Halt or call any exchange endpoint that writes an order. Price-only behavior remains
the comparator and fallback. Model/source failures fail closed and do not authorize new positions.
