# Phase 12 Research Evaluation: Freqtrade and CCXT

- Reviewed: 2026-10-18
- Status: Source review complete; isolated runtime PoC and benchmark not executed
- Decision record: [ADR 0006](../adr/0006-freqtrade-ccxt-engineering-decision.md)

## Capability and integration matrix

| Evaluation item | Freqtrade | CCXT | EVORYN disposition |
|---|---|---|---|
| Latest release observed | 2026.9, 2026-09-29 | 4.5.88, 2026-10-14 | Pin exact release before PoC |
| License observed | GPLv3 | MIT | License notices and distribution model reviewed before use |
| Python compatibility | Declares >=3.12 | Python bindings available; exact release is to be pinned | Isolated environment first |
| Main strength | Strategy backtesting, hyperopt and research workflows | Unified exchange method layer and provider-specific API classes | Keep responsibilities distinct |
| Dependency cost | High: scientific stack, TA-Lib, database, API/web/socket components and CCXT itself | Moderate-to-high transitive library footprint | Do not install either into the core environment now |
| Official exchange evidence for Nobitex | Not listed in the current Freqtrade supported-exchange table | Nobitex support not verified against the pinned release during this review | No presumed capability |
| Spot research | Good candidate to benchmark against EVORYN baselines | Does not provide strategy research by itself | Freqtrade research-only |
| Margin semantics | Exchange-specific mode table matters; current documented list does not establish Nobitex delegated Margin support | Unified margin functions vary by venue and implementation | Use EVORYN's direct Nobitex models until verified |
| Runtime execution | Bot has order execution capability by design | Exchange classes can expose private API calls | Never enable live keys or trading in Phase 12 |
| Proposed decision | **integrate as research tool** | **adapter only** | As documented by ADR 0006 |

## Source facts verified

1. Freqtrade official releases: https://github.com/freqtrade/freqtrade/releases
   - `2026.9` is the latest listed release at review time (2026-09-29).
2. Freqtrade official package metadata: https://github.com/freqtrade/freqtrade/blob/develop/pyproject.toml
   - Python >=3.12, GPLv3, and a broad dependency set including `ccxt`, `pandas`, `numpy`, `TA-Lib`, `scipy`, `SQLAlchemy`, FastAPI/Pydantic and HTTP/WebSocket packages.
3. Freqtrade official exchange feature matrix: https://docs.freqtrade.io/en/latest/exchanges/
   - Specifies tested venue/mode/stop-loss combinations; Nobitex is not included in the current published support table. Out-of-table venues may sometimes operate, but that is not a capability guarantee.
4. CCXT official release list: https://github.com/ccxt/ccxt/releases
   - `4.5.88` is the latest listed release at review time (2026-10-14).
5. CCXT manual: https://github.com/ccxt/ccxt/wiki/Manual
   - Common API methods are implemented per exchange; actual capabilities differ by venue. Adapter contract tests must verify method presence and semantics.
6. CCXT Python license: https://github.com/ccxt/ccxt/blob/master/python/LICENSE.txt
   - MIT License.

## Value and total cost considerations

### Freqtrade

**Value:** fast access to a mature research/backtesting workflow and a candidate comparison baseline for Phase 13. It could reduce time spent building generic backtesting utilities before EVORYN's own assumptions and metrics are validated.

**Costs/risks:** dependency overlap with EVORYN, potentially expensive native TA-Lib/system installation, version drift, a separate release and security-update cycle, translating datasets/strategy parameters/results, output reproducibility, and GPLv3 review. Freqtrade is not proposed as EVORYN's execution engine or risk gate.

**Boundary:** run an immutable release in a separately built container/venv. Supply only versioned historical datasets. No keys, writable exchange configuration, private API access, or live order route.

### CCXT

**Value:** common method names and error handling can reduce adapter boilerplate when a future venue is actually supported.

**Costs/risks:** a general exchange library brings many provider implementations and continuing maintenance; normalization, symbol mapping, precision/limits, rate limits, exception categories and capability mismatches still belong to each adapter. A unified method name cannot establish equivalent venue semantics.

**Boundary:** optional plugin/adaptor. Keep all CCXT types inside `src/evoryn/adapters/exchanges`; translate to EVORYN-owned contracts. For Nobitex Margin, require evidence for the exact relevant endpoints and fields, not just that the library contains generic margin methods.

## Reproducible PoC/benchmark plan (not yet run)

### Dataset contract

- Use synthetic or properly licensed historical OHLCV fixtures with timezone-aware UTC timestamps.
- Record source, license, symbols, interval, missing-candle treatment, observation cutoff, SHA-256 manifest, and a frozen dataset version.
- Make temporal train/validation/test slices disjoint and ordered. Do not random-shuffle time series.
- Preserve completed-candle boundaries and prevent look-ahead, survivorship and leakage.

### Test matrix

| Experiment | Baseline | Candidate | Acceptance evidence |
|---|---|---|---|
| Data parity | EVORYN normalized candles | Freqtrade-imported candles | Same rows, timestamps, symbol and OHLCV after explicit mapping |
| Strategy parity | Simple EVORYN trend/breakout baseline | Equivalent Freqtrade strategy | Same candle set, signal timing and documented costs |
| Cost sensitivity | EVORYN fee/spread/slippage assumptions | Same assumptions in Freqtrade | Quantified metric delta, no hidden defaults |
| Out-of-sample stability | EVORYN walk-forward | Freqtrade walk-forward/sliced runs | Temporal isolation and reproducible results |
| Accounting outputs | EVORYN metrics | Freqtrade exports | Explain differences in drawdown, expectancy, turnover, fees and partial fills |
| Runtime | Current EVORYN workflow | Pinned isolated image | Clean build log, execution time, CPU/RAM, repeat-run determinism |
| Security/licensing | EVORYN SBOM | Candidate SBOM and license set | Dependency audit, license review, no secrets/no network order route |
| CCXT contract | EVORYN read-only Nobitex adapter fixtures | Candidate CCXT adapter | Exact endpoint/capability/error/precision/limit/rate-limit contract; no live writes |

### Release gate

The PoC is successful only if the same input dataset and cost assumptions yield reproducible outputs, the candidate adds material measured research value, all differences are explained, license review is acceptable, and the sandbox contains no credentials and cannot submit a live order. A failed or unrepeatable PoC is grounds to reject further integration.

## Current limitations

- No package was installed and no runtime benchmark was executed in this phase.
- The Desktop Commander usage quota was exhausted in the current environment; it is not used as verification evidence.
- GitHub Actions log/artifact access has been unreliable. Tests and benchmarks are not described as passed or executed without observable results.
- This research note documents the decision inputs and a reproducible experiment plan; it does not claim production or exchange compatibility.


## Future Intelligence phases: open-source-only model selection policy

For later market-regime and NLP/news phases (roadmap Phases 14 and 16), EVORYN will not
train a general model from scratch as the default plan. The evaluation shortlist must start
from existing open-source releases and model repositories on Hugging Face or GitHub. For
each candidate, record the exact revision/model card, license and redistribution/use
terms, supported framework/runtime, language and domain coverage, memory/latency/cost,
reproducibility, dataset/provenance constraints, security history, and an isolated offline
evaluation against a simple EVORYN baseline. "Open source" is not inferred from a repository
being public: the license must explicitly permit the intended use.

Models remain advisory only. They cannot change risk limits, authorize Margin/Spot orders,
clear Auto-Halt, or write directly to exchange adapters. Regime detection must emit an
explicit unknown/abstain state when confidence, freshness, or out-of-sample evidence is
insufficient. News/NLP must preserve source, publication/receipt timestamps, language,
deduplication and stale/manipulated-source indicators. No model is adopted merely because
it performs well on a single backtest.
