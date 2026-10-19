# Phase 19: Shadow Mode — Operator and Runtime Guide

## Status and safety envelope

Phase 19 is currently a **library/API implementation branch**, not an activated live polling service. The implementation provides a bounded REST `poll_once` operation but does not start a continuous scheduler or claim that this conversation collected live market data. Use it only after the Phase 18 verification debt, source/permission review and runtime prerequisites below are assessed.

This path is read-only. It has no order submit/cancel API and never calls an exchange write method. No real orders were sent during development. A recorded target quantity is a virtual analytical projection, not a RiskEngine approval, exchange order, Paper fill or counterfactual fill.

## Integration topology

```text
Existing allowlisted Nobitex Spot candle adapter
        -> bounded completed-candle REST window
        -> ShadowPollingService.poll_once()
        -> freshness / identity / gap / Auto-Halt / regime gates
        -> candidate target + active Paper Champion target
        -> SHADOW decision snapshot
        -> persistent LearningLabLedger (append-only, fsync, hash chain)
```

No WebSocket implementation is included; the feed wraps the existing `NobitexExchangeAdapter.get_market_candles` read-only contract. Do not add a second provider endpoint or bypass the existing official-host policy as part of Phase 19.

## Required runtime inputs

Before calling `poll_once`, provision each of the following using real, immutable artifacts. Placeholder IDs and fabricated SHAs are not valid runtime configuration.

1. A `LearningLabLedger` backed by an access-controlled persistent JSONL path. The ledger must reopen and verify before starting. In-memory-only ledgers are rejected.
2. An `ExperimentRegistry` sharing that exact ledger and persisted spec store.
3. A registered `ExperimentSpec` in `SHADOW` mode for the candidate and a registered `ExperimentSpec` in `PAPER` mode for the current Paper Champion.
4. A `ChampionChallengerController` restored from the same registry/ledger. Its active champion pointer must equal the supplied PAPER baseline ID.
5. Candidate and baseline strategy implementations whose strategy IDs, versions and parameter maps match their respective registered specs. Candidate/baseline must share data version, feature schema, evaluation-config fingerprint, seed and train/validation/test splits.
6. A reviewed regime classifier using the existing abstention contract, a read-only Auto-Halt state provider, and a read-only `ShadowRiskAssessmentPort`. A risk-assessment exception/invalid result/reject/halt/abstain never advances virtual state. Lack of halt or risk state is not interpreted as pass.
7. An explicitly approved Spot market, interval and data source. `NobitexRestCandleFeed` rejects unexpected market/interval/source and current, incomplete candles.

The registered experiment's `dataset_version` and `evaluation_config_fingerprint` identify the static training/research evaluation settings shared by candidate and baseline. The Shadow runtime's virtual budget/allocation/quantity-step policy is recorded separately as `projection_config_fingerprint`, and the current streaming feed contract as `feed_manifest_fingerprint`. Do not silently replace a Paper Champion's historical evaluation fingerprint with a new Shadow projection-policy fingerprint or label a stream as an immutable historical dataset.

## API integration shape

This is an API sketch, not a self-contained executable and not a declaration that real specs/model state are present. All named dependencies must already exist and pass the gates above.

```python
from datetime import timedelta

from evoryn.adapters.exchanges.nobitex import NobitexExchangePlugin
from evoryn.domain.models import Market
from evoryn.shadow import (
    NobitexRestCandleFeed,
    ShadowPollingService,
    ShadowRiskAssessmentPort,
)

market = Market(
    symbol="BTC/USDT",
    venue="nobitex",
    market_type="spot",
)

# Market data only. Keep ENABLE_REAL_TRADING false; the adapter's writes are hard-disabled.
read_only_adapter = NobitexExchangePlugin().build_adapter({
    "NOBITEX_BASE_URL": "https://apiv2.nobitex.ir",
    "ENABLE_REAL_TRADING": "False",
})

feed = NobitexRestCandleFeed(
    read_only_adapter,
    market=market,
    interval=timedelta(minutes=1),
    lookback_candles=100,
    source_version="nobitex-rest-ohlcv/v1",
)

# Build ShadowRunner only from real persisted, verified SHADOW and active PAPER Champion specs.
# shadow_runner = ShadowRunner(
#     config=reviewed_shadow_config,
#     experiment_spec=registered_shadow_spec,
#     baseline_experiment_spec=active_paper_champion_spec,
#     registry=experiment_registry,
#     strategy=registered_shadow_strategy,
#     baseline_strategy=registered_paper_strategy,
#     champion_controller=paper_champion_controller,
#     regime_classifier=reviewed_abstaining_regime_classifier,
#     risk_assessor=reviewed_read_only_shadow_risk_assessor,
#     auto_halt_status=read_only_auto_halt_status,
# )
# service = ShadowPollingService(feed=feed, runner=shadow_runner)
# result = service.poll_once()
```

Never put exchange credentials, URLs with secret query parameters, response bodies or headers in a Shadow snapshot or log. Public candle reads should not be given account/write credentials by default.

## Startup and processing sequence

1. Reopen the Learning Lab JSONL ledger and verify the chain. Load the immutable registry/spec artifacts; the run must fail closed if a spec is not backed by its exact ledger registration.
2. Confirm the candidate is `SHADOW`, the baseline is `PAPER`, the controller points to that same active Champion, and candidate/baseline identities and evaluation assumptions are compatible.
3. Fetch one bounded history window. The service filters the forming candle. Warm-up candles are recorded as a startup reconciliation event and are not emitted as live decision signals; only the latest completed candle is processed for the initial decision.
4. Each later polling call processes only candles newer than the last accepted candle. Duplicate or out-of-order candles are rejected; a gap latches a continuity block.
5. Both candidate and active Paper baseline receive the same completed history. Their target exposure and virtual quantities are computed under the same virtual capital, allocation and quantity-step settings. Any candidate/baseline output failure rejects the whole comparison.
6. A successful read-only risk assessment and successful ledger append must happen before local virtual positions or history advance.
7. The scheduler should invoke `poll_once` at a cadence appropriate to the selected candle interval. The caller must provide cancellation/health supervision and avoid overlapping poll invocations; a scheduler is not bundled with this library.
8. Archive operational metadata and the exact code/spec/registry fingerprints. Never promote Shadow data to Paper evidence automatically.

## Abstention, errors and recovery

- Missing/invalid/active Auto-Halt status: record a rejection and emit no target.
- UNKNOWN, stale/future or identity-changed regime: record abstention.
- Stale/future/malformed/mismatched candles: record rejection.
- Duplicate/out-of-order candle: record rejection and do not update virtual state.
- Candle gap: latch continuity. Provide a documented reconciliation reference and a fresh contiguous series through `reset_after_reconciliation` before resuming.
- Candidate/baseline identity drift or Paper Champion pointer change: latch the run; register a fresh experiment and construct a new runner after review.
- Ledger append/read/verification failure: stop processing. Do not continue with in-memory-only evidence or create a second ledger silently.
- Strategy confidence is unavailable from the existing `CausalTargetStrategy` protocol and is recorded as null. Do not substitute regime confidence for strategy confidence.
- `risk_assessment_status=NOT_EVALUATED` appears only when an earlier gate blocked a target before assessment. For a computed target, the injected `ShadowRiskAssessmentPort` must return an explicit observational pass; otherwise the result is reject/abstain. The snapshot records the assessor's sanitized reason code separately from its short explanation. No production risk assessor was supplied or exercised here. Even `observational_pass` is not an exchange-order or Live RiskEngine authorization.
- No counterfactual fills are simulated. Estimated notional and target quantities are not observed orders, executions, or evidence of profitability.

## Verification and rollout gate

The Phase 19 test source includes mocks; its execution status is **not yet verified**. Before release, run on one identifiable trusted runner:
- `python -m pytest -vv`
- `ruff check .`
- `mypy src tests`
- `bandit -q -r src`
- `pip-audit`

Record runner identity, Python version, exact commit SHA, commands, exit codes and artifact IDs. Current Phase 18 CI evidence shows jobs failed before starting (`runner_id=0`, no name, no steps); do not interpret that as these commands passing.

Independent review is required for the persistent ledger, Champion pointer binding, data provenance, feed/strategy identity and recovery behavior before the Shadow poller is scheduled against a live endpoint. No production readiness or performance claims are authorized by this library implementation.
