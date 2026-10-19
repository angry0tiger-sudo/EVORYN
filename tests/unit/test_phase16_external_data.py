"""Network-free Phase 16 tests for licenses, connectors, news quality, NLP and ablation."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from evoryn.adapters.data.license_registry import SourceLicenseRegistry
from evoryn.adapters.data.market_aggregators import (
    CoinGeckoConnector,
    CoinMarketCapConnector,
)
from evoryn.adapters.data.news_connector import LicensedNewsJsonConnector, NewsFieldMapping
from evoryn.adapters.data.news_corpus import NewsEventCorpus
from evoryn.adapters.data.news_quality import NewsQualityGate, NewsQualityPolicy
from evoryn.adapters.network.json_http import HttpsJsonTransport, JsonApiError
from evoryn.domain.external_data import (
    DataUse,
    NewsArticleCandidate,
    NewsIngestionStatus,
    NewsSentimentRecord,
    SentimentLabel,
    SourceLicenseAuthorization,
)
from evoryn.domain.models import Market, MarketCandle
from evoryn.intelligence.news_sentiment import (
    FINANCIAL_NEWS_MODEL_CARD,
    HuggingFaceNewsSentimentAdapter,
    NewsSentimentOutcome,
)
from evoryn.learning_lab.backtest import BacktestConfig, chronological_split
from evoryn.learning_lab.historical_data import HistoricalDataset
from evoryn.learning_lab.news_ablation import (
    AblationConclusion,
    NewsAblationPolicy,
    NewsAblationReportStore,
    evaluate_news_ablation,
)
from evoryn.strategies.baseline import SmaTrendBaseline


NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
DATA_START = datetime(2026, 10, 1, tzinfo=UTC)
INTERVAL = timedelta(hours=1)
MARKET = Market(symbol="BTC-USDT", venue="nobitex", market_type="spot")
NEWS_API_URL = "https://newsapi.org/terms"
MODEL_REVISION = "b986b74bfd4963613dcbc38d701747eda656ded4"
DATASET_REVISION = "8d3fe0c36d5feec6b3cc5e455b0fcb4820fb9964"


class FakeJsonTransport:
    """Fake provider network: it records requests and never opens a socket."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    def get_json(
        self,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, str],
    ) -> Any:
        self.calls.append({"url": url, "headers": dict(headers), "params": dict(params)})
        return self.payload


class FakeSentimentRuntime:
    """Deterministic three-way model output; it does not import Transformers or torch."""

    def __init__(self, probabilities: dict[str, float] | None = None) -> None:
        self.probabilities = probabilities or {
            "negative": 0.82,
            "neutral": 0.10,
            "positive": 0.08,
        }
        self.inputs: list[str] = []
        self.model_calls: list[tuple[str, str]] = []

    def predict_probabilities(
        self,
        text: str,
        *,
        model_id: str,
        revision: str,
        max_token_count: int,
    ) -> dict[str, float]:
        assert max_token_count > 0
        self.inputs.append(text)
        self.model_calls.append((model_id, revision))
        return self.probabilities


def auth(
    source_id: str,
    base_url: str,
    *,
    uses: frozenset[DataUse] = frozenset({DataUse.NEWS_METADATA}),
    trust: str = "0.95",
    languages: frozenset[str] = frozenset({"en"}),
    retention_days: int | None = None,
) -> SourceLicenseAuthorization:
    retained = (
        30
        if DataUse.LOCAL_CONTENT_STORAGE in uses and retention_days is None
        else (0 if retention_days is None else retention_days)
    )
    return SourceLicenseAuthorization(
        source_id=source_id,
        provider_name=source_id,
        base_url=base_url,
        license_id=f"{source_id}-license-v1",
        license_uri=(
            "https://www.apache.org/licenses/LICENSE-2.0"
            if source_id.startswith("model-")
            else "https://pro.coinmarketcap.com/user-agreement-commercial/"
            if source_id == "coinmarketcap"
            else "https://support.coingecko.com/articles/1043089972-what-are-the-differences-between-commercial-and-custom-licenses"
            if source_id == "coingecko"
            else NEWS_API_URL
        ),
        terms_uri=NEWS_API_URL,
        terms_revision="terms-2026-10-v1",
        accepted_by="phase16-test-reviewer",
        accepted_at=DATA_START - timedelta(days=10),
        review_due_at=NOW + timedelta(days=365),
        permitted_uses=uses,
        attribution_text=f"Data source: {source_id}.",
        trust_score=Decimal(trust),
        allowed_languages=languages,
        active=True,
        raw_content_retention_days=retained,
    )


def news_uses() -> frozenset[DataUse]:
    return frozenset(
        {
            DataUse.NEWS_METADATA,
            DataUse.NEWS_TEXT_PROCESSING,
            DataUse.LOCAL_CONTENT_STORAGE,
            DataUse.INTERNAL_RESEARCH,
        }
    )


def news_registry(*, trust: str = "0.95") -> SourceLicenseRegistry:
    return SourceLicenseRegistry(
        [
            auth(
                "licensed-news",
                "https://newsapi.org/v2",
                uses=news_uses(),
                trust=trust,
                languages=frozenset({"en"}),
            )
        ]
    )


def digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def candidate(
    *,
    now: datetime = NOW,
    article_id: str = "article-001",
    source_id: str = "licensed-news",
    revision: str | None = "revision-1",
    title: str = "Bitcoin market outlook after macro news",
    summary: str = "Analysts review market conditions and potential impact on spot liquidity.",
    published_at: datetime | None = None,
    declared_hash: str | None = None,
    related_asset_ids: tuple[str, ...] = ("BTC-USDT",),
    language: str = "en",
) -> NewsArticleCandidate:
    item = {
        "id": article_id,
        "title": title,
        "summary": summary,
        "url": f"https://publisher.example/articles/{article_id}",
    }
    source_auth = news_registry().get(source_id)
    return NewsArticleCandidate(
        source_id=source_id,
        provider_article_id=article_id,
        source_name="Licensed Publisher",
        article_url=f"https://publisher.example/articles/{article_id}",
        published_at=published_at or now - timedelta(minutes=30),
        retrieved_at=now,
        language=language,
        title=title,
        summary=summary,
        provider_revision=revision,
        source_license_id=source_auth.license_id,
        source_terms_revision=source_auth.terms_revision,
        attribution_text=source_auth.attribution_text,
        payload_sha256=digest(item),
        declared_content_sha256=declared_hash,
        related_asset_ids=related_asset_ids,
    )


def accepted_article(
    *,
    published_at: datetime,
    article_id: str = "article-001",
    revision: str = "revision-1",
    title: str = "Bitcoin market outlook after macro news",
) -> tuple[SourceLicenseRegistry, Any]:
    registry = news_registry()
    source = registry.get("licensed-news")
    item = {
        "id": article_id,
        "title": title,
        "summary": "A material macro event may affect Bitcoin spot liquidity.",
        "url": f"https://publisher.example/articles/{article_id}",
    }
    candidate_record = NewsArticleCandidate(
        source_id="licensed-news",
        provider_article_id=article_id,
        source_name="Licensed Publisher",
        article_url=f"https://publisher.example/articles/{article_id}",
        published_at=published_at,
        retrieved_at=published_at,
        language="en",
        title=title,
        summary="A material macro event may affect Bitcoin spot liquidity.",
        provider_revision=revision,
        source_license_id=source.license_id,
        source_terms_revision=source.terms_revision,
        attribution_text=source.attribution_text,
        payload_sha256=digest(item),
        related_asset_ids=("BTC-USDT",),
    )
    result = NewsQualityGate(license_registry=registry).evaluate(
        candidate_record, now=published_at
    )
    assert result.status is NewsIngestionStatus.ACCEPTED
    assert result.article is not None
    return registry, result.article


def sentiment_record(article, *, analyzed_at: datetime) -> NewsSentimentRecord:
    return NewsSentimentRecord(
        article=article,
        analyzed_at=analyzed_at,
        label=SentimentLabel.NEGATIVE,
        positive_probability=Decimal("0.08"),
        neutral_probability=Decimal("0.10"),
        negative_probability=Decimal("0.82"),
        model_id="synthetic-news-sentiment",
        model_revision=MODEL_REVISION,
        model_license_id="Apache-2.0",
        model_card_uri=FINANCIAL_NEWS_MODEL_CARD,
        model_training_dataset_id="takala/financial_phrasebank",
        model_training_dataset_revision=DATASET_REVISION,
        model_training_dataset_license_id="CC-BY-NC-SA-3.0",
        model_training_data_rights_verified=True,
        commercial_use_approved=False,
    )


def sentiment_review(
    *,
    rights_verified: bool = True,
    commercial_approved: bool = False,
):
    return HuggingFaceNewsSentimentAdapter.financial_phrasebank_candidate(
        reviewed_by="explicit-test-review",
        reviewed_at=DATA_START - timedelta(days=1),
        review_due_at=NOW + timedelta(days=30),
        training_data_rights_verified=rights_verified,
        commercial_use_approved=commercial_approved,
    )


def test_coinmarketcap_official_api_requires_license_and_keeps_key_out_of_url() -> None:
    transport = FakeJsonTransport(
        {
            "data": {
                "1": {
                    "name": "Bitcoin",
                    "symbol": "BTC",
                    "last_updated": "2026-10-19T11:59:00.000Z",
                    "quote": {
                        "USD": {
                            "price": "65000.25",
                            "market_cap": "1280000000000",
                            "volume_24h": "28000000000",
                            "last_updated": "2026-10-19T11:59:00.000Z",
                        }
                    },
                }
            }
        }
    )
    license_registry = SourceLicenseRegistry(
        [
            auth(
                "coinmarketcap",
                "https://pro-api.coinmarketcap.com",
                uses=frozenset({DataUse.MARKET_REFERENCE, DataUse.INTERNAL_RESEARCH}),
            )
        ]
    )
    connector = CoinMarketCapConnector(
        transport=transport,
        license_registry=license_registry,
        api_key="test-secret-do-not-log",
    )

    quote = connector.get_quote(1, convert="USD", now=NOW)

    assert quote.provider_asset_id == "1"
    assert quote.price == Decimal("65000.25")
    assert quote.observed_at < quote.retrieved_at
    assert quote.source_terms_revision == "terms-2026-10-v1"
    assert "test-secret-do-not-log" not in transport.calls[0]["url"]
    assert transport.calls[0]["headers"]["X-CMC_PRO_API_KEY"] == "test-secret-do-not-log"


def test_coingecko_adapter_uses_stable_provider_slug_and_official_header() -> None:
    transport = FakeJsonTransport(
        [
            {
                "id": "bitcoin",
                "symbol": "btc",
                "name": "Bitcoin",
                "current_price": 65000,
                "market_cap": 1280000000000,
                "total_volume": 28000000000,
                "last_updated": "2026-10-19T11:58:00.000Z",
            }
        ]
    )
    registry = SourceLicenseRegistry(
        [
            auth(
                "coingecko",
                "https://api.coingecko.com/api/v3",
                uses=frozenset({DataUse.MARKET_REFERENCE, DataUse.INTERNAL_RESEARCH}),
            )
        ]
    )
    connector = CoinGeckoConnector(
        transport=transport,
        license_registry=registry,
        api_key="demo-secret",
    )

    quote = connector.get_quote("bitcoin", vs_currency="usd", now=NOW)

    assert quote.provider_asset_id == "bitcoin"
    assert quote.price == Decimal("65000")
    assert transport.calls[0]["url"] == "https://api.coingecko.com/api/v3/coins/markets"
    assert transport.calls[0]["headers"]["x-cg-demo-api-key"] == "demo-secret"
    assert "demo-secret" not in transport.calls[0]["url"]


def test_no_registered_permission_means_no_external_request() -> None:
    transport = FakeJsonTransport({"articles": []})
    registry = SourceLicenseRegistry(
        [
            auth(
                "licensed-news",
                "https://newsapi.org/v2",
                uses=frozenset({DataUse.MARKET_REFERENCE}),
            )
        ]
    )
    connector = LicensedNewsJsonConnector(
        source_id="licensed-news",
        endpoint_path="everything",
        field_mapping=NewsFieldMapping(
            items_path="articles",
            article_id_path="id",
            source_name_path="source.name",
            article_url_path="url",
            published_at_path="publishedAt",
            language_path="language",
            title_path="title",
            summary_path="description",
        ),
        transport=transport,
        license_registry=registry,
        base_params={"language": "en"},
    )

    with pytest.raises(PermissionError):
        connector.fetch_candidates(now=NOW)

    assert transport.calls == []


def test_news_connector_derives_and_labels_missing_provider_article_id() -> None:
    transport = FakeJsonTransport(
        {
            "articles": [
                {
                    "source": {"name": "Licensed Publisher"},
                    "url": "https://publisher.example/story/no-id",
                    "publishedAt": "2026-10-19T11:30:00Z",
                    "language": "en",
                    "title": "Bitcoin market outlook after macro news",
                    "description": "A licensed story summary on Bitcoin spot markets.",
                }
            ]
        }
    )
    connector = LicensedNewsJsonConnector(
        source_id="licensed-news",
        endpoint_path="everything",
        field_mapping=NewsFieldMapping(
            items_path="articles",
            article_id_path=None,
            source_name_path="source.name",
            article_url_path="url",
            published_at_path="publishedAt",
            language_path="language",
            title_path="title",
            summary_path="description",
        ),
        transport=transport,
        license_registry=news_registry(),
        base_params={"language": "en"},
    )

    record = connector.fetch_candidates(now=NOW)[0]

    assert record.provider_id_evidence == "url_sha256"
    assert record.provider_article_id.startswith("url-sha256:")
    assert len(record.provider_article_id.removeprefix("url-sha256:")) == 64


def test_licensed_news_json_connector_records_provider_and_retrieval_provenance() -> None:
    transport = FakeJsonTransport(
        {
            "articles": [
                {
                    "id": "n-1",
                    "source": {"name": "Licensed Publisher"},
                    "url": "https://publisher.example/story/1",
                    "publishedAt": "2026-10-19T11:30:00Z",
                    "language": "en",
                    "title": "Bitcoin market outlook after macro news",
                    "description": "A licensed story summary on Bitcoin spot markets.",
                    "revision": "r7",
                    "assets": ["BTC-USDT"],
                }
            ]
        }
    )
    registry = news_registry()
    connector = LicensedNewsJsonConnector(
        source_id="licensed-news",
        endpoint_path="everything",
        field_mapping=NewsFieldMapping(
            items_path="articles",
            article_id_path="id",
            source_name_path="source.name",
            article_url_path="url",
            published_at_path="publishedAt",
            language_path="language",
            title_path="title",
            summary_path="description",
            revision_path="revision",
            related_asset_ids_path="assets",
        ),
        transport=transport,
        license_registry=registry,
        base_params={"language": "en"},
        secret_header_name="X-NEWS-KEY",
        secret_value="secret-news-key",
    )

    candidates = connector.fetch_candidates(now=NOW)

    assert len(candidates) == 1
    item = candidates[0]
    assert item.provider_article_id == "n-1"
    assert item.published_at == datetime(2026, 10, 19, 11, 30, tzinfo=UTC)
    assert item.retrieved_at == NOW
    assert item.language == "en"
    assert item.language_evidence == "provider_field"
    assert item.related_asset_ids == ("BTC-USDT",)
    assert transport.calls[0]["url"] == "https://newsapi.org/v2/everything"
    assert "secret-news-key" not in transport.calls[0]["url"]
    assert "secret-news-key" == transport.calls[0]["headers"]["X-NEWS-KEY"]


def test_news_connector_language_fallback_must_match_explicit_request_filter() -> None:
    payload = {
        "articles": [
            {
                "id": "n-2",
                "source": {"name": "Licensed Publisher"},
                "url": "https://publisher.example/story/2",
                "publishedAt": "2026-10-19T11:30:00Z",
                "title": "Bitcoin market outlook with licensed metadata",
                "description": "The API query explicitly requested English-language results.",
            }
        ]
    }
    mapping = NewsFieldMapping(
        items_path="articles",
        article_id_path="id",
        source_name_path="source.name",
        article_url_path="url",
        published_at_path="publishedAt",
        language_path="language",
        title_path="title",
        summary_path="description",
    )
    registry = news_registry()
    transport = FakeJsonTransport(payload)
    connector = LicensedNewsJsonConnector(
        source_id="licensed-news",
        endpoint_path="everything",
        field_mapping=mapping,
        transport=transport,
        license_registry=registry,
        base_params={"language": "en"},
        language_fallback="en",
    )

    item = connector.fetch_candidates(now=NOW)[0]

    assert item.language == "en"
    assert item.language_evidence == "request_filter"


def test_https_json_transport_rejects_unlisted_host_before_io() -> None:
    transport = HttpsJsonTransport(allowed_hosts=frozenset({"newsapi.org"}))
    with pytest.raises(JsonApiError, match="allowlist"):
        transport.get_json(
            "https://attacker.example/data",
            headers={},
            params={},
        )


def test_news_quality_gate_rejects_stale_weak_and_integrity_mismatched_candidates() -> None:
    stale_registry = news_registry()
    stale = NewsQualityGate(license_registry=stale_registry).evaluate(
        candidate(published_at=NOW - timedelta(days=3)), now=NOW
    )
    assert stale.status is NewsIngestionStatus.REJECTED
    assert stale.reason_code == "ARTICLE_TOO_OLD"

    weak_registry = news_registry(trust="0.2")
    weak = NewsQualityGate(license_registry=weak_registry).evaluate(
        candidate(), now=NOW
    )
    assert weak.reason_code == "SOURCE_TRUST_BELOW_MINIMUM"

    bad_hash = NewsQualityGate(license_registry=news_registry()).evaluate(
        candidate(declared_hash="0" * 64), now=NOW
    )
    assert bad_hash.status is NewsIngestionStatus.SUSPICIOUS
    assert bad_hash.reason_code == "DECLARED_CONTENT_HASH_MISMATCH"


def test_news_quality_gate_detects_duplicates_and_unversioned_content_mutation() -> None:
    registry = news_registry()
    gate = NewsQualityGate(license_registry=registry)
    initial_candidate = candidate()
    first = gate.evaluate(initial_candidate, now=NOW)
    duplicate = gate.evaluate(initial_candidate, now=NOW)
    mutated = gate.evaluate(
        candidate(summary="The same provider article changed materially without a revision."),
        now=NOW,
    )

    assert first.status is NewsIngestionStatus.ACCEPTED
    assert duplicate.status is NewsIngestionStatus.DUPLICATE
    assert duplicate.reason_code == "SAME_PROVIDER_ID_AND_CONTENT"
    assert mutated.status is NewsIngestionStatus.SUSPICIOUS
    assert mutated.reason_code == "CONTENT_MUTATED_WITHOUT_NEW_PROVIDER_REVISION"


def test_new_provider_revision_can_be_accepted_but_old_records_are_not_silently_rewritten() -> None:
    gate = NewsQualityGate(license_registry=news_registry())
    first = gate.evaluate(candidate(revision="rev1"), now=NOW)
    updated = gate.evaluate(
        candidate(revision="rev2", summary="The provider explicitly published a new revision."),
        now=NOW,
    )

    assert first.status is NewsIngestionStatus.ACCEPTED
    assert updated.status is NewsIngestionStatus.ACCEPTED
    assert updated.article is not None
    assert updated.article.provider_revision == "rev2"


def test_news_quality_gate_rejects_active_markup_and_future_publication() -> None:
    gate = NewsQualityGate(license_registry=news_registry())
    markup = gate.evaluate(
        candidate(title="Bitcoin market <script>alert(1)</script> warning"),
        now=NOW,
    )
    future = gate.evaluate(
        candidate(published_at=NOW + timedelta(minutes=1)),
        now=NOW,
    )

    assert markup.status is NewsIngestionStatus.SUSPICIOUS
    assert markup.reason_code == "ACTIVE_CONTENT_MARKERS"
    assert future.status is NewsIngestionStatus.SUSPICIOUS
    assert future.reason_code == "PUBLICATION_TIME_IN_FUTURE"


def test_news_corpus_requires_retention_permission_and_purges_on_expiry() -> None:
    registry, article = accepted_article(published_at=NOW - timedelta(minutes=10))
    corpus = NewsEventCorpus(registry)
    corpus.append_article(article, now=NOW)
    snapshot = corpus.snapshot(now=NOW)
    assert snapshot.articles == (article,)

    expired_at = NOW + timedelta(days=31)
    removed_articles, removed_sentiments = corpus.purge_expired(now=expired_at)
    assert removed_articles == 1
    assert removed_sentiments == 0
    assert corpus.snapshot(now=expired_at).articles == ()


def test_sentiment_model_candidate_is_blocked_without_training_data_rights() -> None:
    registry, article = accepted_article(published_at=NOW - timedelta(minutes=10))
    runtime = FakeSentimentRuntime()
    adapter = HuggingFaceNewsSentimentAdapter(
        review=sentiment_review(rights_verified=False),
        source_license_registry=registry,
        runtime=runtime,
    )

    with pytest.raises(PermissionError, match="Training-data rights"):
        adapter.analyze(article, now=NOW)

    assert runtime.inputs == []


def test_huggingface_sentiment_adapter_is_pinned_local_and_source_license_gated() -> None:
    registry, article = accepted_article(published_at=NOW - timedelta(minutes=10))
    runtime = FakeSentimentRuntime()
    adapter = HuggingFaceNewsSentimentAdapter(
        review=sentiment_review(rights_verified=True),
        source_license_registry=registry,
        runtime=runtime,
    )

    outcome = adapter.analyze(article, now=NOW)

    assert not outcome.abstained
    assert outcome.record is not None
    assert outcome.record.label is SentimentLabel.NEGATIVE
    assert outcome.record.confidence == Decimal("0.82")
    assert outcome.record.model_revision == MODEL_REVISION
    assert outcome.record.model_training_dataset_revision == DATASET_REVISION
    assert outcome.record.model_training_dataset_license_id == "CC-BY-NC-SA-3.0"
    assert runtime.model_calls == [
        (
            "mrm8488/distilroberta-finetuned-financial-news-sentiment-analysis",
            MODEL_REVISION,
        )
    ]
    assert runtime.inputs == [f"{article.title}\n{article.summary}"]
    assert article.article_url not in runtime.inputs[0]


def test_sentiment_adapter_abstains_on_invalid_distribution_and_unsupported_language() -> None:
    registry, article = accepted_article(published_at=NOW - timedelta(minutes=10))
    bad = HuggingFaceNewsSentimentAdapter(
        review=sentiment_review(rights_verified=True),
        source_license_registry=registry,
        runtime=FakeSentimentRuntime(
            {"negative": 0.8, "neutral": 0.8, "positive": 0.8}
        ),
    )
    invalid = bad.analyze(article, now=NOW)
    assert isinstance(invalid, NewsSentimentOutcome)
    assert invalid.abstained
    assert invalid.reason_code == "INVALID_PROBABILITY_DISTRIBUTION"

    from dataclasses import replace

    persian_article = replace(article, language="fa")
    unsupported = HuggingFaceNewsSentimentAdapter(
        review=sentiment_review(rights_verified=True),
        source_license_registry=registry,
        runtime=FakeSentimentRuntime(),
    ).analyze(persian_article, now=NOW)

    assert unsupported.abstained
    assert unsupported.reason_code == "LANGUAGE_UNSUPPORTED"


def ablation_dataset(count: int = 120) -> HistoricalDataset:
    candles: list[MarketCandle] = []
    prior_close = Decimal("100")
    crash_index = 100
    for index in range(count):
        if index < crash_index:
            close = Decimal("100") + Decimal(index) / Decimal("2")
            open_price = prior_close
        elif index == crash_index:
            open_price = prior_close
            close = Decimal("70")
        else:
            open_price = prior_close
            close = Decimal("70")
        candles.append(
            MarketCandle(
                market=MARKET,
                timestamp=DATA_START + INTERVAL * index,
                interval=INTERVAL,
                open=open_price,
                high=max(open_price, close) + Decimal("1"),
                low=min(open_price, close) - Decimal("1"),
                close=close,
                volume=Decimal("10000"),
                source="synthetic-phase16-ablation",
            )
        )
        prior_close = close
    return HistoricalDataset(
        dataset_id="synthetic-phase16-ablation",
        market=MARKET,
        interval=INTERVAL,
        volume_unit="base_asset",
        source_name="Synthetic Phase 16 fixture",
        source_uri="urn:evoryn:synthetic-phase16-ablation:v1",
        source_revision="synthetic-v1",
        license_id="CC0-1.0",
        license_uri="https://spdx.org/licenses/CC0-1.0.html",
        collected_at=candles[-1].timestamp + INTERVAL + timedelta(minutes=1),
        candles=tuple(candles),
    )


def test_news_ablation_respects_available_at_and_uses_the_same_test_range(
    tmp_path: Path,
) -> None:
    dataset = ablation_dataset()
    partitions = chronological_split(dataset)
    crash_time = dataset.candles[100].timestamp
    # Publication, retrieval and analysis are all completed before the simulated bar opens.
    news_available_at = crash_time - timedelta(minutes=30)
    registry, article = accepted_article(
        published_at=news_available_at,
        article_id="negative-before-crash",
        title="Unexpected macro event causes Bitcoin market concern",
    )
    # Receipt and inference complete at the next-open decision time; no future feature is used.
    news = sentiment_record(article, analyzed_at=news_available_at)
    report = evaluate_news_ablation(
        dataset,
        partitions=partitions,
        price_only_strategy=SmaTrendBaseline(window=3),
        sentiment_records=(news,),
        license_registry=registry,
        config=BacktestConfig(
            initial_cash=Decimal("10000"),
            commission_bps=Decimal("10"),
            spread_bps=Decimal("5"),
            slippage_bps=Decimal("5"),
            max_bar_participation=Decimal("0.5"),
            allocation_fraction=Decimal("0.9"),
            quantity_step=Decimal("0.001"),
        ),
        evaluated_at=NOW,
        policy=NewsAblationPolicy(minimum_effective_news_events=1),
    )

    assert report.test_start_index == partitions.test.start
    assert report.test_end_index_exclusive == partitions.test.end
    assert report.baseline_result.evaluation_range == partitions.test
    assert report.news_result.evaluation_range == partitions.test
    assert report.news_articles_available_in_test == 1
    assert report.news_articles_actually_vetoed == 1
    assert report.veto_decision_count >= 1
    assert report.news_result.strategy_id == "price-baseline-with-negative-news-veto"
    assert report.conclusion is AblationConclusion.VALUE_ADDED_SUPPORTED
    assert report.news_result.metrics.net_return_fraction >= (
        report.baseline_result.metrics.net_return_fraction
    )
    assert report.news_result.metrics.max_drawdown_fraction < (
        report.baseline_result.metrics.max_drawdown_fraction
    )

    store = NewsAblationReportStore(tmp_path)
    path = store.save(report)
    assert store.save(report) == path
    stored = store.load(report.report_id)
    assert stored["report_id"] == report.report_id
    assert stored["test_range"]["start"] == partitions.test.start
    assert stored["news_evidence"]["models"][0]["training_dataset_license_id"] == (
        "CC-BY-NC-SA-3.0"
    )
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="checksum"):
        store.load(report.report_id)


def test_news_ablation_never_uses_news_before_its_actual_availability() -> None:
    dataset = ablation_dataset()
    partitions = chronological_split(dataset)
    # The article is published/retrieved/analyzed after the test decision time. It must be inert.
    future_time = dataset.candles[110].timestamp + timedelta(minutes=10)
    registry, article = accepted_article(
        published_at=future_time,
        article_id="future-news",
        title="Future information cannot affect a past trading decision",
    )
    news = sentiment_record(article, analyzed_at=future_time)
    report = evaluate_news_ablation(
        dataset,
        partitions=partitions,
        price_only_strategy=SmaTrendBaseline(window=3),
        sentiment_records=(news,),
        license_registry=registry,
        config=BacktestConfig(
            initial_cash=Decimal("10000"),
            commission_bps=Decimal("10"),
            spread_bps=Decimal("5"),
            slippage_bps=Decimal("5"),
            max_bar_participation=Decimal("0.5"),
            allocation_fraction=Decimal("0.9"),
            quantity_step=Decimal("0.001"),
        ),
        evaluated_at=NOW,
        policy=NewsAblationPolicy(minimum_effective_news_events=1),
    )

    assert report.news_articles_actually_vetoed == 0
    assert report.veto_decision_count == 0
    assert report.news_result.metrics == report.baseline_result.metrics
    assert report.conclusion is AblationConclusion.INCONCLUSIVE
