# ADR 0006: Freqtrade and CCXT Integration Boundaries

- Status: Accepted (architecture/domain decision; not live-trading authorization)
- Date checked: 2026-10-18
- Scope: Roadmap Phase 12
- Decision vocabulary: adopt / integrate as research tool / adapter only / reject
- Governing constraints: EVORYN remains a modular monolith with explicit ports; risk,
  execution and audit ledgers remain under EVORYN control; no dependency is selected for
  popularity alone.

## Decision summary

| Candidate | Decision | Why |
|---|---|---|
| Freqtrade | **integrate as research tool** | Valuable backtesting, hyperparameter search and strategy-baseline capability, but too broad to embed in EVORYN's core. Run as a separate, isolated research process with no live keys. |
| CCXT | **adapter only** | Useful common exchange API vocabulary behind an EVORYN-owned adapter, but feature support is exchange- and endpoint-specific. Do not make it a domain or core runtime dependency; Nobitex capability is not presumed. |

Neither candidate is approved as an autonomous live execution path. No new production dependency is added by this ADR.

## Evidence checked on 2026-10-18

### Freqtrade

- Official releases: https://github.com/freqtrade/freqtrade/releases
  - Latest listed stable release at review time: `2026.9`, published 2026-09-29.
  - Release notes include exchange-clock offset checks and startup validation for requested candle types unsupported by an exchange. These are useful safeguards, not proof of EVORYN compatibility.
- Official dependency metadata: https://github.com/freqtrade/freqtrade/blob/develop/pyproject.toml
  - Requires Python >=3.12, compatible at the version floor with EVORYN's Python >=3.12 setting.
  - Declares GPLv3.
  - Carries a substantial dependency surface including CCXT, SQLAlchemy, pandas, NumPy, TA-Lib, SciPy, FastAPI, Pydantic, HTTP/WebSocket clients and other utilities. The exact tree changes over time and must be inspected at a pinned release before a PoC.
- Official installation requirements: https://github.com/freqtrade/freqtrade/blob/develop/docs/installation.md
- Backtesting docs: https://docs.freqtrade.io/en/latest/backtesting/
- Exchange support matrix: https://docs.freqtrade.io/en/latest/exchanges/
  - The matrix explicitly distinguishes tested/supported exchange-mode and stop-loss capabilities.
  - The current published support table does not list Nobitex. Freqtrade documentation cautions that exchanges outside its supported list may work, but certain operational capabilities such as exchange-side stop-loss may be unavailable. This is not sufficient evidence to use it as the Nobitex execution adapter.

### CCXT

- Official releases: https://github.com/ccxt/ccxt/releases
  - Latest listed release at review time: `4.5.88`, published 2026-10-14.
- Official manual: https://github.com/ccxt/ccxt/wiki/Manual
  - The unified exchange API reduces boilerplate, but supported methods and semantics vary by exchange. The manual exposes capability checks such as `exchange.has`; generic method presence is not proof that a venue implements a required endpoint correctly.
- Python package license: https://github.com/ccxt/ccxt/blob/master/python/LICENSE.txt
  - MIT License. Preserve the copyright and license notice in distributions containing CCXT.
- Nobitex-specific support: **not verified by this review** against the pinned CCXT release. Do not treat this unknown as a positive capability. EVORYN's explicit Nobitex adapter and documented market semantics remain the source of truth until a contract test proves otherwise.

## Architecture fit and operating cost

### Freqtrade as a research tool

Use it in a separate container/process and a dedicated Python environment. Exchange credentials must not be present, and any configuration that can submit real orders must be omitted or explicitly disabled. Exchange access is not part of the first PoC.

Permitted direction of data flow:

1. EVORYN exports a versioned, sanitized historical candle dataset with a manifest, timestamp convention, symbol mapping, and data-license reference.
2. Freqtrade produces backtest results and metadata in its own isolated environment.
3. EVORYN imports only schema-validated, versioned outputs into its own evaluation and learning workflow.
4. EVORYN independently reproduces the metrics it relies on or rejects results that cannot be explained.

Do not import Freqtrade internals into the EVORYN runtime, subclass its trading bot, or make EVORYN risk/selection/execution depend on its object model. Do not copy code from the GPLv3 project into EVORYN without a separate license review. Running a separate program does not remove every legal question about how components are combined or redistributed; consult counsel before packaging or distributing an integrated product.

### CCXT as an optional adapter

If a future adapter PoC justifies it, place CCXT behind EVORYN's existing `ExchangePort`. Convert its provider-specific output to EVORYN-owned immutable models, and map errors/rate limits/precision explicitly. Do not leak CCXT classes through core domain contracts. The adapter must declare per-exchange capabilities and fail closed when a method is missing, unsupported, or semantically incompatible.

For Nobitex Margin, generic CCXT leverage/margin methods are insufficient proof. The adapter would need to demonstrate that the selected pinned CCXT version exposes the exact required Nobitex endpoints and preserves position, liability, extension-fee, margin-ratio, and liquidation-price semantics. Until then, the dedicated read-only Nobitex normalizer remains authoritative.

## Security, maintenance, and license assessment

| Dimension | Freqtrade | CCXT |
|---|---|---|
| Intended role | Isolated backtesting/research process | Optional provider adapter behind ExchangePort |
| License | GPLv3; distribution and combination require deliberate review | MIT; retain notices |
| Runtime/dependency surface | Large; creates dependency conflict and update work if installed into the EVORYN environment | Broad exchange library, but can be limited to adapter-specific packaging |
| Exchange support | Explicit supported-mode matrix; Nobitex not listed in current official table | Dynamic exchange classes/capabilities; Nobitex capability not verified in this review |
| Security boundary | No keys, no live exchange routes; sandbox/network isolation; pin release and image digest | No core access to credentials; least-privilege credentials only for a future separately approved adapter test |
| Update policy | Review release notes and dependency/SBOM changes before updating isolated research image | Pin exact version; contract/failure tests required before adapter upgrades |
| Ongoing ownership | Research-image maintenance, dataset/output schema, reproducibility, license review | Maintain translations, venue capability map, rate-limit semantics, precision rules and contract tests |
| Reversal | Drop isolated research image and import adapter; EVORYN runtime remains unchanged | Disable plugin and retain EVORYN's direct provider adapter |

## Required PoC before any wider adoption

This ADR is a source-based architecture decision, **not a claim that a runtime PoC or benchmark has already run**. A future isolated PoC must record:

- Exact Freqtrade release/image digest and exact CCXT release if used.
- A checksum-addressed synthetic or sanitized historical dataset and its license.
- EVORYN baseline vs Freqtrade metrics with identical candles, trading costs, position sizing, timeframe, and train/validation/test date boundaries.
- Results for fees, spread, slippage assumptions, partial-fill limitations, drawdown, expectancy, turnover, and reproducibility.
- Cold installation/runtime duration, dependency/SBOM/security audit, resource use, and any version conflict.
- Explicit confirmation that the PoC had no credentials and could not invoke a live order endpoint.
- For CCXT, per-method capability evidence and contract tests against offline fixtures or an explicitly approved sandbox; no live Margin call in this phase.

Acceptance: reproducible metrics, material value over EVORYN's existing baseline, clean licensing review, and no weakening of EVORYN risk, reconciliation, or audit contracts. Otherwise reject or keep the tool outside the architecture.

## Consequences

### Positive

- Avoids coupling EVORYN's domain, risk and execution layers to a third-party bot's internal abstractions.
- Reuses existing backtesting capabilities without making EVORYN's runtime inherit the entire research tool dependency stack.
- Provides a route to CCXT's common exchange interface without assuming every endpoint exists.

### Negative / trade-offs

- Isolated environments, data adapters, version pinning and duplicated contract tests add maintenance work.
- Freqtrade's GPLv3 licensing needs attention before redistribution/combined packaging.
- Nobitex support remains unproven for both the Freqtrade supported-exchange matrix and CCXT's current release in this review.

## Safety boundary

This ADR does not install dependencies, add exchange credentials, create a live execution path, approve a Margin adapter, or authorize real trading. Any future code integration requires a dedicated PR, pinned versions, capability evidence, tests, and review.
