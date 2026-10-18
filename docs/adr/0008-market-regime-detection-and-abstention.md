# ADR 0008: Market-Regime Detection with Open-Source Research Models and Abstention

- Status: Proposed for review
- Date: 2026-10-19
- Scope: Roadmap Phase 14
- Depends on: Phase 13 versioned datasets and chronological evaluation; ADR 0006 open-source
  model policy

## Decision

1. Add a regime research interface with four known labels: `bull_trend`, `bear_trend`,
   `range_bound`, and `high_volatility`. `unknown` is a fail-closed abstention state, not a
   fifth learned state.
2. Establish an auditable rule-based baseline and an optional four-state Gaussian HMM adapter
   using the existing open-source `hmmlearn` project from GitHub.
3. Keep HMM dependencies in the optional `regime` extra only; none is required to import the core
   EVORYN package. The current range is `hmmlearn==0.3.3`, `numpy>=1.26,<2`, and
   `scikit-learn>=1.5,<2`. The trained model manifest records exact Python, hmmlearn, NumPy,
   SciPy and scikit-learn versions, dataset digest, feature version, train range, random seed,
   fitted state mapping, state counts and model-parameter fingerprint.
4. HMM fitting accepts a Phase 13 `TemporalPartitions` object and an exact
   `HistoricalDataset` version. The fitted range must be a subrange of that partition's train
   interval, and both the dataset digest and candle count must match. Validation/test slices are
   rejected by the public fitting API. Features are scaled using only train features, and the
   same stored mean/scale are used for inference.
5. For inference, the caller supplies a chronological prefix that starts at the fitted training
   range's first candle and ends at the latest completed candle. The adapter passes only that
   prefix to `predict_proba`; no later bar is available to smooth the latest posterior.
6. Map latent HMM states to semantic labels only from train-state means: highest realized
   volatility -> high volatility; highest/lowest trend strength among remaining states -> bull/
   bear; remaining state -> range. This interpretation is heuristic, not supervised ground truth.
   Model fitting is refused if it does not converge, fitted state occupancy is insufficient,
   parameters are non-finite, or trend-state mapping is ambiguous.
7. Return `UNKNOWN` when history is insufficient, gapped, mixed, stale, future-dated, the model
   is unready, output probabilities are invalid, or the highest probability is below the explicit
   minimum-confidence threshold. The default threshold is 0.65 and the default freshness gate is
   two candle intervals. Both threshold values are emitted with every prediction.
8. A prediction records as-of time, label, top posterior/score, all four label probabilities
   when available, abstention reason, model ID/version/fingerprint, feature schema, confidence
   threshold and freshness setting.
9. Keep the rule baseline's scores explicitly marked as uncalibrated. They are for comparison,
   not evidence that the model is reliable and not a substitute for validation calibration.
10. Neither the rule baseline nor the HMM adapter imports or receives broker/exchange ports,
    Spot execution, Margin models, risk decisions or kill-switch controls. Regime output remains
    advisory. It cannot alter hard risk caps, clear Auto-Halt, authorize orders or write to an
    exchange.

## Open-source model/library evaluation

### GitHub: hmmlearn

- Source: https://github.com/hmmlearn/hmmlearn
- License shown by the upstream repository: BSD-3-Clause.
- Purpose: existing unsupervised Hidden Markov Model implementation; EVORYN does not reimplement
  the HMM algorithm.
- Compatibility: upstream documents NumPy and scikit-learn dependencies, requires a compiler for
  source builds, and has supported Python 3.12 in its recent compatibility history.
- Maintenance risk: the upstream README explicitly describes the project as being in
  limited-maintenance mode. The optional adapter must therefore remain replaceable, versioned,
  and non-critical. No inference service or per-request hosting fee is incurred; compute cost is
  local CPU/RAM and training time.
- Adoption decision: integrate as an optional research model only. Do not make it a base dependency
  or claim production readiness without a version-pinned environment and walk-forward evaluation.

### Hugging Face: AAdevloper/market-regime-classifier

- Model card: https://huggingface.co/AAdevloper/market-regime-classifier
- Reviewed revision: `7325a77fe660bfe08798ca05a32afd012c65c57c`
- Listed license: MIT.
- Declared model: XGBoost binary `RISK_ON/RISK_OFF` for Indian/NIFTY 50 conditions with India VIX,
  RSI-14, MA-50 and MA-200 features. The model card says its training data is synthetic NIFTY 50
  data and shows no filled-in quantitative ML/backtest result tables.
- Suitability decision: research reference only; reject direct EVORYN deployment. The domain,
  feature schema and two-class taxonomy do not establish a reliable four-state classifier for
  Nobitex crypto Spot. The card references a separate MLflow registry URI; a self-contained
  reproducible model artifact and its exact feature/data lineage have not been verified in this
  review. We will not call its hosted API or promote it on the strength of the MIT license alone.

A public repository or model card is not enough to establish that its weights, training data, and
all dependencies are redistributable or suitable. Before any future Hugging Face candidate is
adopted, record an immutable revision, weight availability, model and dataset licenses, source
data lineage, compatible framework versions, memory/latency/cost, feature parity and a
reproducible evaluation. No network inference endpoint is used in Phase 14.

## Feature schema and timing

Version `1.0.0` contains:
- Mean simple return over the trailing lookback.
- Cumulative trend strength over the same lookback.
- Population standard deviation of returns as realized-volatility proxy.
- Relative last-candle volume versus its trailing-window mean.

All inputs are canonical OHLCV candles. Timestamps represent candle-open time; each feature row
is as-of the last candle's close time. The feature extractor rejects market/interval/source
mixing, non-chronological timestamps and missing intervals. It never repairs gaps silently.

## Validation and acceptance gates

- Compare the transparent rule baseline and HMM on the same licensed/versioned dataset and
  feature windows.
- Fit scaling, hidden states and the regime-label mapping only on each train window.
- Evaluate on chronologically later validation/test segments and rolling walk-forward folds.
- Report per-class confusion/coverage, abstention rate, calibration, label persistence, regime
  transitions, class/occupancy balance, and downstream descriptive metrics without assigning
  execution authority to the model.
- Test sensitivity to lookback/state count/seed, data gaps and changing volatility; confirm
  unknown/abstain behavior under stale inputs and invalid outputs.
- Do not interpret the HMM's latent-state posterior as a calibrated probability of economic
  regime truth until held-out calibration has been measured.

## Current evidence boundary

The phase adds implementation and synthetic unit fixtures, not a real-market regime claim.
No licensed real-market dataset is currently embedded, no HMM training run is asserted, and no
backtest or CI test is reported as passed unless a verified runner output becomes available.
The regime adapter is research-only; Spot/Margin execution and risk modules are unchanged.
