"""Explicit, fail-closed license/terms registry for external providers and model inputs."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from threading import RLock

from evoryn.domain.external_data import DataUse, SourceLicenseAuthorization


class SourceLicenseRegistry:
    """In-memory license evidence registry; no source is usable by default."""

    def __init__(self, authorizations: Iterable[SourceLicenseAuthorization] = ()) -> None:
        self._lock = RLock()
        self._authorizations: dict[str, SourceLicenseAuthorization] = {}
        for authorization in authorizations:
            self.register(authorization)

    def register(self, authorization: SourceLicenseAuthorization) -> None:
        """Register one terms snapshot; replacement requires an explicit new registry instance."""
        if not isinstance(authorization, SourceLicenseAuthorization):
            raise TypeError(
                "Only validated SourceLicenseAuthorization records may be registered."
            )
        with self._lock:
            if authorization.source_id in self._authorizations:
                raise ValueError(
                    f"Source {authorization.source_id!r} already has a license record."
                )
            self._authorizations[authorization.source_id] = authorization

    def get(self, source_id: str) -> SourceLicenseAuthorization:
        """Return source terms evidence or fail when no explicit record exists."""
        with self._lock:
            try:
                return self._authorizations[source_id]
            except KeyError as exc:
                raise PermissionError(
                    f"No source license authorization is registered for {source_id!r}."
                ) from exc

    def require(
        self,
        source_id: str,
        use: DataUse,
        *,
        now: datetime,
    ) -> SourceLicenseAuthorization:
        """Require current authorization for the exact requested use."""
        authorization = self.get(source_id)
        authorization.authorize(use, now=now)
        return authorization

    @property
    def source_ids(self) -> tuple[str, ...]:
        """Return stable metadata only, never keys or article content."""
        with self._lock:
            return tuple(sorted(self._authorizations))

    def manifest(self) -> tuple[dict[str, object], ...]:
        """Return an auditable summary of registered permissions and review lifetime."""
        with self._lock:
            return tuple(
                {
                    "source_id": source.source_id,
                    "provider_name": source.provider_name,
                    "base_url": source.base_url,
                    "license_id": source.license_id,
                    "license_uri": source.license_uri,
                    "terms_uri": source.terms_uri,
                    "terms_revision": source.terms_revision,
                    "accepted_by": source.accepted_by,
                    "accepted_at": source.accepted_at.isoformat(),
                    "review_due_at": source.review_due_at.isoformat(),
                    "permitted_uses": sorted(use.value for use in source.permitted_uses),
                    "attribution_text": source.attribution_text,
                    "trust_score": str(source.trust_score),
                    "allowed_languages": sorted(source.allowed_languages),
                    "active": source.active,
                    "raw_content_retention_days": source.raw_content_retention_days,
                }
                for source in sorted(
                    self._authorizations.values(), key=lambda item: item.source_id
                )
            )
