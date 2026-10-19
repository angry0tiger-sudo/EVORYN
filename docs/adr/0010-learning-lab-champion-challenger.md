# ADR 0010: Learning Lab Experiment Registry, Champion/Challenger and Paper Rollback

- Status: Proposed for review
- Date: 2026-10-19
- Scope: Roadmap Phase 17
- Depends on: Phase 13 versioned backtests, Phase 14 regime metadata, Phase 16 license/data provenance,
  and the enterprise release pipeline

## Context

Model and parameter changes can overfit a holdout, be evaluated against different fee assumptions,
or be promoted because an artifact merely exists. They must not write code into the Live path,
change a hard risk cap, or disable Auto-Halt. The learning system needs one immutable record of what
was tested, where it was tested, which evidence supported a change, and what happened afterward.

## Decision

### 1. Experiment identity and immutable registry

- An experiment specification records a 40-character Git commit SHA, exact historical dataset SHA-256
  version, feature-schema version, evaluation-config fingerprint, model ID/version/fingerprint,
  deterministic parameter key/value pairs, seed, train/validation/test half-open index ranges,
  mode, and UTC creation time.
- The experiment fingerprint hashes code/data/model/features/parameters/seed/split/mode and the
  evaluation configuration. The unique experiment ID also incorporates UTC creation time, allowing
  repeated trials to remain separate while preserving the same content fingerprint.
- Evaluation configuration must include every economically material assumption, including fees,
  spread, slippage, fill limits, initial cash/allocation, engine/version and other simulation settings.
  Champion and Challenger must use the same config fingerprint and test range for a valid comparison.
- Specs and run evidence are stored as canonical JSON beneath immutable content-derived IDs. Existing
  conflicting bytes, altered fields, mismatched run identities or an unregistered artifact are rejected.
- Backtest replay evidence can only be created for a BACKTEST experiment and must match the registered
  dataset, test range, exact simulator configuration fingerprint and strategy parameters. Backtest
  evidence is diagnostic; it cannot itself become Paper telemetry or authorize promotion.

### 2. Learning Lab Ledger

- Each lifecycle event records a sequence, event ID, correlation ID, event type, experiment ID,
  UTC time, canonical payload and payload digest.
- The optional JSONL ledger uses append-only writes, `fsync`, a SHA-256 event chain, identity-hash
  validation, sequence/link checks and full verification when reopened. A partial line, changed
  payload, hash mismatch or missing evidence event fails closed.
- Experiment registration, run evidence, candidate comparisons, accepted/rejected changes, Paper
  health assessments, rollback, release candidate creation, risk-policy rejection, human approval and
  attempted Live release are recorded.
- If a crash leaves an immutable file without its matching ledger event, the file is not trusted for
  promotion. An exact, verified registration/evidence can be reconciled into the ledger on retry;
  conflicting content cannot be repaired or overwritten.
- SHA-256 hash chaining is tamper-evident against accidental changes, not a cryptographic signature
  against a privileged party capable of rewriting the entire file and chain. Production deployment
  still requires protected storage, access controls, backups and independent integrity checks.

### 3. Champion/Challenger gates

- A Champion is an existing PAPER experiment. Challenger evidence must match the exact experiment ID,
  model identity, data version, feature schema, evaluation configuration, seed, and test range.
- Comparisons are allowed only in SHADOW or PAPER. BACKTEST evidence is not accepted as telemetry
  for a Paper/Shadow promotion.
- Default minimum evidence: 100 observations and 10 closed trades for both Champion and Challenger.
- Default performance gates: Challenger return improvement at least 0.5 percentage points, maximum
  drawdown no greater than 20%, no more than 1 percentage point of drawdown worsening, net loss no
  greater than 5%, zero risk-gate denials, and both arms pass data-quality checks. These are explicit
  conservative policy defaults, not venue facts or guarantees of profitability.
- The evaluation policy limits comparisons to five per identical dataset-version/test-range/mode.
  The controller counts earlier comparison events in the ledger. Once the limit is reached, the
  result is `INSUFFICIENT_EVIDENCE`, preventing indefinite repeated selection against one holdout.
  Operators must register a new preregistered, untouched holdout rather than changing the threshold
  after seeing results.
- A passing SHADOW candidate records approval for Shadow only; it never changes the Paper Champion.
  A passing PAPER candidate updates the internal Paper champion pointer while retaining the prior
  champion ID as the rollback target. This is a registry state transition, not a code deployment.

### 4. Automatic Paper rollback

- Rollback triggers are maximum drawdown above 20%, net return below -5%, failed data quality,
  any risk-gate denial (default maximum zero), or an experiment/model/data identity mismatch.
- A healthy or insufficient window is also recorded in the Learning Lab Ledger. Fewer than 20
  observations defaults to `INSUFFICIENT_EVIDENCE` unless a hard data-quality, risk-denial or identity
  failure requires an immediate safe action.
- On a trigger, the controller restores the last known-good Paper Champion. If no predecessor is
  available, the active Paper selection is disabled and status becomes `SAFE_HOLD_NO_CHAMPION`.
  It does not change RiskEngine limits, clear Auto-Halt, change any Live configuration or submit orders.

### 5. Release evidence and human approval

- A release candidate is limited to SHADOW or PAPER and must reference a successful, ledgered
  Champion/Challenger decision for that exact experiment and target mode.
- Its manifest records experiment ID, promotion decision ID, test-report hash, security-report hash,
  immutable approved risk-policy hash, creation time and, after independent approval, actor,
  approval reference/ticket and approval timestamp.
- A changed risk-policy hash rejects candidate creation or approval. Rejected attempts are recorded.
- An attempted LIVE target is recorded as `LIVE_RELEASE_BLOCKED` and raises a permission error. There
  is no deploy operation or Live target approval API in this module.
- This gate records the hash and approval evidence, but does not fetch/parse test/security reports or
  authenticate the human identity. The external protected Release Pipeline must verify the referenced
  artifact bytes, CI/security attestations, approval ticket and branch/review rules before any
  non-Live rollout. Approval in this module is not deployment authorization.

## Data model and interfaces

- `ExperimentSpec`: immutable inputs and split identity.
- `ExperimentRunEvidence`: immutable result snapshot, mode, origin, metrics and risk/data-quality facts.
- `ExperimentRegistry`: append-only spec/evidence JSON store with content and ledger verification.
- `LearningLabLedger`: hash-chained audit log with replay verification.
- `ChampionChallengerGate`: deterministic, preregistered comparison thresholds.
- `ChampionChallengerController`: Paper champion pointer, Shadow approval, comparison logging and rollback.
- `ReleasePipelineGate`: Paper/Shadow release evidence and independent human approval record only.

## Security boundary

No machine-learning model receives write access to the repository, CI permissions, exchange connectors,
RiskEngine limits, kill-switch APIs, or Live configuration. Phase 17 computes an advisory release
decision and internal Paper/Shadow registry state only. A human-controlled, protected Release Pipeline
is the sole route for authorized deployment decisions; no release path to Live is implemented here.

## Validation and current evidence limits

Synthetic tests cover identity/version mismatch, immutable artifact corruption, ledger chain mutation,
matched/mismatched cost assumptions, repeated-holdout comparison cap, Paper promotion, Shadow isolation,
automatic rollback/safe-hold, release decision linkage, risk-policy drift, and Live blocking. These tests
are authored but must not be described as passed without verified runner output. No real market,
Paper or Shadow telemetry is included by this phase, and no model improvement or profitability claim is
made.

## Rollback

Remove the Phase 17 experiment registry, promotion module, tests and this ADR. Preserve prior Phase
13/14/16 research artifacts. The code adds no dependencies and has no live execution or risk-policy
mutation route.
