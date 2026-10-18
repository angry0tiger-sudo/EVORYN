# Phase 14: Market-Regime Research and Abstention

## Safety boundary

This package is advisory-only. It does not submit orders, edit RiskLimits, clear Auto-Halt,
change the portfolio, import an exchange adapter, or invoke Spot/Margin execution. `UNKNOWN`
means no usable classification was issued. Consumers must handle UNKNOWN as a no-action research
state rather than forcing it into one of the learned labels.

## Regimes and features

Canonical model labels are `bull_trend`, `bear_trend`, `range_bound`, and
`high_volatility`. `unknown` is reserved for abstention.

Feature schema `1.0.0` has:
- Mean simple return over the trailing lookback.
- Cumulative trend strength over that lookback.
- Population standard deviation of simple returns.
- Current completed candle volume relative to trailing mean volume.

Feature rows become available at the end of the last candle, never before. The extractor rejects
gaps, duplicate/out-of-order times, mixed markets, intervals, or sources. Missing history is not
filled using future candles. Volume ratio is dimensionless; the current version uses the
source-provided volume values consistently and does not assume cross-venue volume units match.

## Open-source implementations

### Transparent comparator

`RuleBasedRegimeBaseline` is deterministic and parameterized by explicit trend/volatility
thresholds. Its normalized scores are **uncalibrated heuristic scores**. Keep it as a baseline;
do not present its scores as statistical confidence.

### Optional Gaussian HMM

The adapter `GaussianHMMRegimeModel` uses the existing GitHub project
[hmmlearn](https://github.com/hmmlearn/hmmlearn), whose repository marks the license BSD-3-Clause.
The upstream README warns it is under limited-maintenance mode and documents NumPy,
scikit-learn and compiler dependencies. To install the optional research dependencies in an
isolated environment, use:

```shell
python -m pip install -e ".[regime]"
```

The `regime` extra is optional. Core EVORYN imports do not require NumPy, scikit-learn or
hmmlearn. Pin the installed versions for every experiment; the HMM manifest records the exact
Python/numerical-library versions to aid reproduction.

### Hugging Face candidate disposition

The reviewed MIT-licensed
[AAdevloper/market-regime-classifier](https://huggingface.co/AAdevloper/market-regime-classifier)
was not adopted as a ready-made EVORYN model. Its card describes synthetic NIFTY 50 training
data, India VIX/RSI/MA50/MA200 features and only two classes, RISK_ON/RISK_OFF. That is not
evidence it will detect four regimes on Nobitex crypto Spot. The card's referenced MLflow URI
and exact self-contained model/data artifacts were not verified here. Phase 14 does not call
the model's hosted endpoint.

## Fitting without leakage

`GaussianHMMRegimeModel.fit` requires:
- the SHA-256 version of the dataset that supplies training candles;
- `split_name="train"`;
- start index and exclusive end index matching the actual supplied candle count.

Fitting on validation/test is rejected. The train-only scaler, HMM state parameters and semantic
state map are fixed before out-of-sample inference. At inference, the history must begin at the
training window's first candle and contain only completed candles up to the current decision time.
The adapter calls `predict_proba` on that prefix only, then returns the last posterior row. No
later candle is included in that call.

Latent state names are assigned from train-only state means: highest volatility -> high volatility;
among remaining states, strongest/weakest trend -> bull/bear; final state -> range. This is only
a heuristic semantic map and must be checked on validation data. The HMM is not a supervised
four-class classifier with verified economic truth labels.

Training fails closed if the optional package is absent, the train split is misdeclared, the
training range is inconsistent, the sequence is invalid, training does not converge, parameters
are non-finite, state occupancy is inadequate, or trend-state mapping is ambiguous. The manifest
records dataset version, exact training range, feature schema/lookback, Python and library versions,
random seed, state mapping/counts and model parameter fingerprint.

## Example: fit on train, then classify a chronological prefix

```python
from decimal import Decimal

from evoryn.intelligence import (
    AbstainingRegimeDetector,
    GaussianHMMRegimeModel,
    RegimeFeatureExtractor,
    RegimeThresholds,
)
from evoryn.learning_lab.backtest import chronological_split
from evoryn.learning_lab.historical_data import HistoricalDatasetStore

dataset = HistoricalDatasetStore("data/historical").load(
    "authorized-dataset-id", "the-64-character-dataset-version"
)
parts = chronological_split(dataset)
features = RegimeFeatureExtractor(lookback=20)
model = GaussianHMMRegimeModel(feature_extractor=features)
manifest = model.fit_dataset(
    dataset,
    split_name="train",
    train_start_index=parts.train.start,
    train_end_index_exclusive=parts.train.end,
)
detector = AbstainingRegimeDetector(
    model,
    feature_extractor=features,
    thresholds=RegimeThresholds(
        min_confidence=Decimal("0.65"),
        max_data_age_intervals=2,
    ),
)
current_index = parts.validation.start + 100
result = detector.classify(
    dataset.candles[parts.train.start : current_index + 1],
    now=dataset.candles[current_index].timestamp
        + dataset.candles[current_index].interval,
)
# Inspect result.label, result.confidence, result.reason_code and result.model_fingerprint.
# UNKNOWN remains UNKNOWN; it is not coerced into a trading signal.
```

The numeric index above is illustrative. Ensure `current_index` is within the dataset and after the
end of the training range, and store `manifest.to_dict()` beside the experiment report.

## Abstention policy

The default `RegimeThresholds` are:
- Minimum top-class probability/score: `0.65`.
- Maximum age: `2` candle intervals after the most recent completed bar.

The detector emits `UNKNOWN` for empty/insufficient history, stale/future candles, gaps or
identity mixing, unready model, invalid probability distributions, or a top score below threshold.
Every prediction records its as-of time, model ID/version/fingerprint, feature schema, actual
threshold values, probabilities when available, abstention flag and reason code. Posterior
confidence itself is not proof of calibration; thresholds must be validated out-of-sample before
the model can be considered suitable even for advisory production display.

## Test and result status

Network-free tests cover trailing feature causality, time gaps, market mixing, threshold/abstention
behavior, stale and future data, invalid probability output, rule-baseline determinism, and the
HMM's train-only API boundary. No real regime accuracy, confidence calibration, backtest uplift or
profitability result is claimed. The current GitHub Actions job-log access issue has prevented
verification of the CI run in this environment; test authorship is not represented as a passing
test run.
