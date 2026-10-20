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
- Reviewed immutable model-card revision: `5219379338751d0bf804161f409167429a3df3d3`
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

## Feature schemas and timing

The production-facing `RegimeFeatureExtractor` uses schema `2.0.0-price-only` by default.
Its legacy four-slot vector retains the volume-ratio field as zero unless callers explicitly
opt into the separate `2.0.0-price-plus-volume` schema; the Nobitex UDF workflow never enables
that opt-in while volume units are unknown. Old model artifacts whose schema does not match the
manifest are rejected.

The offline challenger pipeline uses schema `3.0.0-price-technical-only` with 21 causal
features: multi-horizon returns; 6/12/24-period realized volatility; SMA gaps; RSI-14; MACD
line/signal/histogram; Bollinger %B/bandwidth; ATR-14; 48-bar trend return; 24-bar range
position; and a short/long volatility ratio. These features are derived from trailing completed
OHLC bars only. The schema contains no volume field. RSI is an internally defined simple
14-delta variant, not a venue-provided indicator.

Timestamps represent candle-open time; every feature row is as-of the last completed candle's
close. Feature generation rejects insufficient or malformed price windows and never repairs
gaps silently. Triple-Barrier labels use decision-time trailing volatility to set symmetric
barriers, a six-bar horizon, and first future OHLC touch; same-candle double touches are
conservatively labelled high volatility because OHLC cannot establish intrabar ordering.
Label windows crossing train/validation/test boundaries are purged.

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

The challenger workflow is research-only. It compares Random Forest, HistGradientBoosting,
XGBoost, LightGBM, a forward-filtered Gaussian HMM, and a compact CPU LSTM under two weak-label policies.
Ten chronological test windows, per-policy fold objects, majority-class metrics, and same-fold
portfolio simulations after assumed commission/spread/slippage costs are persisted.

The recorded challenge produced no model passing the conjunctive acceptance gate: accuracy,
balanced accuracy and macro-F1 improvements over the train-only majority baseline, positive
net-return difference versus same-fold Buy-and-Hold, at least twenty closed trades, and full
fold coverage. No selected model is wired into runtime. Even a future candidate that passes
this gate needs an untouched confirmation period, a schema-compatible loader with independently
pinned artifact/manifest digests, and independent review.

Nobitex data-use rights remain unreviewed. OHLCV spread/slippage are assumptions; unknown volume
units disable capacity caps. No account endpoint, live order, or exchange-write route is part of
this workflow. Spot/Margin execution and hard risk modules remain unchanged.


## Exact-head research result — 2026-10-20

The final candidate SHA `c08096a8ff8c28d78a9879977cc5c30fde2d5f77` passed six-gate CI [#39397941134](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39397941134) and research run [#39397941187](https://github.com/angry0tiger-sudo/EVORYN/actions/runs/39397941187). See [the complete report](../research/2026-10-20-regime-challenger-walk-forward.md). Train-only majority Accuracy averaged 63.062% for forward-threshold labels and 63.445% for Triple-Barrier labels; all five candidates had lower mean Accuracy than the Triple-Barrier baseline. No model passed the combined classification/economic gate; no model is wired into runtime. Research schema v3 remains separate from the production loader contract.


## Six-model evaluation status — 2026-10-20

LightGBM is included as an optional, pinned research challenger. The retrieved six-model artifact
confirms that all six models have fold-level classification outputs, but its schema-version-2
report omits policy-specific backtests for the forward-threshold label policy. Treat those
economic comparisons as incomplete in that artifact. The schema-version-3 pipeline and a fresh
exact-head artifact must be checked before closing the reporting gate. No model has been selected
for runtime; `selected_model_for_runtime=null` remains mandatory.
