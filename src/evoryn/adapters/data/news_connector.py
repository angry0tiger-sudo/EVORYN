"""Licensed JSON-news connector that never scrapes publisher/article pages."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

from evoryn.adapters.data.license_registry import SourceLicenseRegistry
from evoryn.adapters.network.json_http import JsonApiTransport
from evoryn.domain.external_data import DataUse, NewsArticleCandidate, require_aware_utc


class NewsConnectorError(ValueError):
    """A provider JSON response that cannot be normalized using the declared schema."""


@dataclass(frozen=True, slots=True)
class NewsFieldMapping:
    """Explicit JSON field paths for one provider's official licensed news endpoint."""

    items_path: str
    article_id_path: str
    source_name_path: str
    article_url_path: str
    published_at_path: str
    language_path: str
    title_path: str
    summary_path: str
    revision_path: str | None = None
    related_asset_ids_path: str | None = None
    declared_content_sha256_path: str | None = None

    def __post_init__(self) -> None:
        for name in (
            "items_path",
            "article_id_path",
            "source_name_path",
            "article_url_path",
            "published_at_path",
            "language_path",
            "title_path",
            "summary_path",
        ):
            _validate_json_path(getattr(self, name), name)
        for name in (
            "revision_path",
            "related_asset_ids_path",
            "declared_content_sha256_path",
        ):
            value = getattr(self, name)
            if value is not None:
                _validate_json_path(value, name)


class LicensedNewsJsonConnector:
    """Fetch only an approved provider JSON endpoint; publisher URLs are metadata only.

    Terms must explicitly permit metadata retrieval. Accepted article storage and NLP processing
    are checked by the separate quality gate and model adapter respectively.
    """

    def __init__(
        self,
        *,
        source_id: str,
        endpoint_path: str,
        field_mapping: NewsFieldMapping,
        transport: JsonApiTransport,
        license_registry: SourceLicenseRegistry,
        base_params: dict[str, str] | None = None,
        secret_header_name: str | None = None,
        secret_value: str | None = None,
    ) -> None:
        if not source_id.strip():
            raise ValueError("source_id must not be empty.")
        if (
            not endpoint_path
            or endpoint_path.startswith("/")
            or endpoint_path.startswith("\\")
            or "://" in endpoint_path
            or "?" in endpoint_path
            or "#" in endpoint_path
            or any(part in {"", ".", ".."} for part in endpoint_path.split("/"))
        ):
            raise ValueError("endpoint_path must be a safe relative path, not a URL.")
        if (secret_header_name is None) != (secret_value is None):
            raise ValueError("Secret header name and value must be provided together.")
        if secret_header_name is not None and (
            not secret_header_name.strip()
            or "\r" in secret_header_name
            or "\n" in secret_header_name
        ):
            raise ValueError("secret_header_name is invalid.")
        if secret_value is not None and not secret_value.strip():
            raise ValueError("secret_value must not be empty.")
        self._source_id = source_id
        self._endpoint_path = endpoint_path
        self._mapping = field_mapping
        self._transport = transport
        self._licenses = license_registry
        self._base_params = dict(base_params or {})
        self._secret_header_name = secret_header_name
        self._secret_value = secret_value

    def fetch_candidates(
        self,
        *,
        now: datetime,
        params: dict[str, str] | None = None,
    ) -> tuple[NewsArticleCandidate, ...]:
        """Fetch and normalize provider-supplied article metadata under active authorization."""
        require_aware_utc(now, "news retrieval time")
        authorization = self._licenses.require(
            self._source_id, DataUse.NEWS_METADATA, now=now
        )
        base_url = authorization.base_url.rstrip("/") + "/"
        url = urljoin(base_url, self._endpoint_path)
        base_parts = urlsplit(authorization.base_url)
        target_parts = urlsplit(url)
        if (
            target_parts.scheme != "https"
            or target_parts.hostname != base_parts.hostname
            or target_parts.username is not None
            or target_parts.password is not None
            or target_parts.fragment
        ):
            raise PermissionError("Configured news endpoint escaped the registered provider host.")

        headers: dict[str, str] = {}
        if self._secret_header_name is not None and self._secret_value is not None:
            headers[self._secret_header_name] = self._secret_value
        request_params = dict(self._base_params)
        request_params.update(params or {})
        payload = self._transport.get_json(url, headers=headers, params=request_params)
        rows = _json_path(payload, self._mapping.items_path)
        if not isinstance(rows, list):
            raise NewsConnectorError("Configured items_path did not resolve to a JSON array.")

        normalized: list[NewsArticleCandidate] = []
        for row in rows:
            if not isinstance(row, dict):
                raise NewsConnectorError("Article item must be a JSON object.")
            title = _string_at(row, self._mapping.title_path, required=True)
            summary = _string_at(row, self._mapping.summary_path, required=False)
            url_value = _string_at(row, self._mapping.article_url_path, required=True)
            published_text = _string_at(row, self._mapping.published_at_path, required=False)
            published_at = _optional_timestamp(published_text)
            revision = _string_at(row, self._mapping.revision_path, required=False)
            language = _string_at(row, self._mapping.language_path, required=True)
            related_ids: tuple[str, ...] = ()
            if self._mapping.related_asset_ids_path is not None:
                raw_related = _json_path(row, self._mapping.related_asset_ids_path)
                if not isinstance(raw_related, list) or any(
                    not isinstance(item, str) or not item.strip() for item in raw_related
                ):
                    raise NewsConnectorError(
                        "related_asset_ids_path must resolve to a string array."
                    )
                related_ids = tuple(sorted({item.strip().upper() for item in raw_related}))
            declared_hash = _string_at(
                row, self._mapping.declared_content_sha256_path, required=False
            )
            provider_id = _string_at(row, self._mapping.article_id_path, required=True)
            source_name = _string_at(row, self._mapping.source_name_path, required=True)
            row_bytes = json.dumps(
                row,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            normalized.append(
                NewsArticleCandidate(
                    source_id=authorization.source_id,
                    provider_article_id=provider_id,
                    source_name=source_name,
                    article_url=url_value,
                    published_at=published_at,
                    retrieved_at=now,
                    language=language.strip().lower(),
                    title=title,
                    summary=summary,
                    provider_revision=revision,
                    source_license_id=authorization.license_id,
                    source_terms_revision=authorization.terms_revision,
                    attribution_text=authorization.attribution_text,
                    payload_sha256=hashlib.sha256(row_bytes).hexdigest(),
                    declared_content_sha256=declared_hash,
                    related_asset_ids=related_ids,
                )
            )
        return tuple(normalized)


def _validate_json_path(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty dotted JSON path.")
    parts = value.split(".")
    if any(not part or part.startswith("_") for part in parts):
        raise ValueError(f"{name} contains an unsafe JSON path segment.")


def _json_path(value: Any, path: str) -> Any:
    result = value
    for part in path.split("."):
        if isinstance(result, dict):
            if part not in result:
                raise NewsConnectorError(
                    f"Provider response omitted configured field path {path!r}."
                )
            result = result[part]
        else:
            raise NewsConnectorError(f"Configured field path {path!r} is not a JSON object path.")
    return result


def _string_at(value: dict[str, Any], path: str | None, *, required: bool) -> str:
    if path is None:
        if required:
            raise NewsConnectorError("A required provider field mapping is not configured.")
        return ""
    try:
        result = _json_path(value, path)
    except NewsConnectorError:
        if not required:
            return ""
        raise
    if result is None and not required:
        return ""
    if not isinstance(result, str):
        if required:
            raise NewsConnectorError(f"Provider field {path!r} must be a string.")
        return ""
    if required and not result.strip():
        raise NewsConnectorError(f"Provider field {path!r} must not be empty.")
    return result.strip()


def _optional_timestamp(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)
