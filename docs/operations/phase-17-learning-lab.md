# Phase 17: Learning Lab, Champion/Challenger and Paper Rollback

## Purpose and safety

Phase 17 records exact experiment inputs/results and compares a candidate against the current Champion
without granting the model execution authority. Everything here is a registry, evidence gate or
release manifest. It does not change strategy code, change RiskEngine limits, clear Auto-Halt, edit
Live configuration or submit orders. The only active pointer managed by the controller is an internal
Paper Champion ID; Shadow candidates are tracked separately.

## Register an experiment

Use `ExperimentSpec` with:

- `code_revision`: full 40-character Git commit SHA.
- `dataset_version`: exact Phase 13 dataset SHA-256.
- `feature_schema_version`, `model_id`, `model_version`, and the SHA-256 `model_fingerprint`.
- `evaluation_config_fingerprint`: SHA-256 over canonical JSON of all evaluation assumptions.
- `parameters`: sorted string key/value pairs.
- `random_seed` and train/validation/test `IndexRange` boundaries.
- `mode`: BACKTEST, SHADOW or PAPER.
- `created_at`: UTC timestamp.

`configuration_fingerprint(mapping)` provides the canonical JSON hash helper. For backtest evidence,
fingerprint `BacktestConfig.as_dict()`; the recorder checks that the result's exact config agrees with
the registered fingerprint. Never reuse a fingerprint after changing commission, spread, slippage,
volume participation, initial capital, allocation, quantity step, feature definition or runtime assumption.
The engine's code revision and run identity are captured separately from this config fingerprint.

Example identity setup:

```python
from datetime import UTC, datetime
from decimal import Decimal

from evoryn.learning_lab.backtest import BacktestConfig, IndexRange
from evoryn.learning_lab.experiment_registry import (
    ExperimentMode,
    ExperimentRegistry,
    ExperimentSpec,
    LearningLabLedger,
    configuration_fingerprint,
)

backtest_config = BacktestConfig(
    initial_cash=Decimal("10000"),
    commission_bps=Decimal("10"),
    spread_bps=Decimal("10"),
    slippage_bps=Decimal("5"),
    max_bar_participation=Decimal("0.05"),
    allocation_fraction=Decimal("0.90"),
    quantity_step=Decimal("0.001"),
    allow_gaps=False,
)
spec = ExperimentSpec(
    code_revision="a" * 40,  # Replace with the real lowercase Git commit SHA.
    dataset_version="b" * 64,  # Replace with the registered historical dataset version.
    feature_schema_version="ohlcv-features/v1",
    evaluation_config_fingerprint=configuration_fingerprint(backtest_config.as_dict()),
    model_id="strategy-or-model-id",
    model_version="1.0.0",
    model_fingerprint="c" * 64,  # Replace with the actual model/strategy artifact hash.
    parameters=(("fast_window", "10"), ("slow_window", "30")),
    random_seed=42,
    train_range=IndexRange(0, 600),
    validation_range=IndexRange(600, 800),
    test_range=IndexRange(800, 1000),
    mode=ExperimentMode.BACKTEST,
    created_at=datetime.now(UTC),
)
ledger = LearningLabLedger("data/learning-lab/ledger.jsonl")
registry = ExperimentRegistry("data/learning-lab/registry", ledger)
experiment_id = registry.register(spec, correlation_id="experiment:run-001")
```

The string above for the Git SHA is illustrative only; the code requires a real lowercase hex SHA.
Use actual immutable IDs in all records. The example is not a training run or a market result.

## Record and verify evidence

`ExperimentRunEvidence` records a complete result snapshot and requires an explicit origin:
`backtest_replay`, `shadow_telemetry`, or `paper_telemetry`. The origin must match the mode.
Backtest replay evidence is created only for a BACKTEST spec, and exact dataset version, test indices,
strategy parameter tuple and evaluation-config fingerprint must match the specification.

All evidence is written under an immutable content-derived JSON filename and also appended to the
Learning Lab Ledger. A missing ledger event makes the evidence unavailable for promotion. On retry,
an intact orphaned file can be reconciled into the ledger; changed/conflicting bytes fail closed.

`ledger.verify()` recomputes event IDs, each payload digest, chain links and event hashes. Persistent
JSONL records are written with append-only mode and `fsync`. The SHA-256 chain detects accidental or
partial modification, but it is not a digital signature against a privileged full-file rewrite.

## Champion/Challenger workflow

The gate requires both evidence records to match their registered specs and to share the same dataset
version, feature schema, evaluation configuration and test range. Default thresholds are:

| Gate | Default |
|---|---:|
| Minimum observations for each arm | 100 |
| Minimum closed trades for each arm | 10 |
| Minimum return improvement | 0.5 percentage points |
| Maximum Challenger drawdown | 20% |
| Maximum drawdown worsening vs Champion | 1 percentage point |
| Maximum net loss | 5% |
| Allowed risk-gate denials | 0 |
| Comparisons per dataset/test-range/mode | 5 |

These are hard, predeclared policy defaults for the research gate, not validated optimal parameters.
The controller counts earlier comparisons recorded in the ledger for the same data version, test indices
and mode. Once the comparison cap is reached, it returns `INSUFFICIENT_EVIDENCE`. Do not keep tuning
against the same holdout; register a new preregistered experiment and independent holdout instead.

Before any comparison can result in promotion, the controller must be configured with a trusted
`EvidenceTrustVerifier` implementation that authenticates the Paper/Shadow telemetry collector or
validates its signed attestation bound to evidence ID, experiment, mode and collection window. If the
verifier is omitted or rejects either arm, the controller writes a rejection to the Learning Lab Ledger
and refuses promotion. The synthetic verifier used in unit tests is not a production trust source.

The active Champion pointer must reference a PAPER spec:
- SHADOW comparison requires a separate SHADOW-mode copy of the Champion with all other inputs equal.
  Approval records the candidate but leaves the Paper Champion pointer untouched.
- PAPER promotion requires a passing PAPER-mode decision. The challenger becomes the Paper Champion;
  the prior champion is retained as the rollback target.
- BACKTEST-only evidence cannot be passed to the Paper/Shadow gate.

## Automatic Paper rollback

The controller monitors a registered PAPER evidence record. The default policy rolls back on any of:

- Maximum drawdown above 20%.
- Net return below -5%.
- Failed data-quality flag.
- Any risk-gate denial.
- Wrong experiment/model/data identity or non-PAPER telemetry.

A window with fewer than 20 observations returns `INSUFFICIENT_EVIDENCE` and does not claim health,
unless hard quality, risk-denial or identity gates already trigger. Every health assessment, including
healthy and insufficient windows, is recorded to the Learning Lab Ledger. When a last-known-good
predecessor exists, the internal Paper Champion pointer reverts to it. Without a predecessor, the
selection enters `SAFE_HOLD_NO_CHAMPION`, with no new candidate enabled.

Rollback does not edit source code or deployment files. An external caller must use the resulting
decision to stop Paper selection or restore the recorded Paper configuration; it must not infer a
Live rollback or modify risk ceilings.

## Release pipeline

`ReleasePipelineGate` will only record SHADOW/PAPER candidates. It requires a matching, ledgered
successful promotion decision for that experiment and target, hashes for test/security reports, and an
approved immutable risk-policy fingerprint. A human approval records reviewer, ticket/reference and UTC
time. If the risk-policy fingerprint changes, creation/approval is rejected and logged. Attempts to use
target `LIVE` are logged as `LIVE_RELEASE_BLOCKED` and raise a permission error.

This module does not inspect report file contents, validate CI status on its own, authenticate the human
approver or deploy artifacts. A protected external Release Pipeline must verify that the report bytes
match their hashes, confirm required CI/security gates, enforce branch protection and approval, and
perform any approved non-Live rollout. Phase 17 contains no API to release to Live.

## Current validation status

Network-free synthetic tests are authored for spec fingerprinting, storage tampering, ledger reload and
chain integrity, evidence identity, repeat-holdout limits, Paper promotion, Shadow isolation, rollback,
safe hold, orphaned artifact reconciliation, release-decision linkage, risk policy drift and Live denial.
No test run is claimed as passed in this document unless a verifiable runner report is attached. No real
Paper/Shadow market data, model improvement or profitability result is included.
