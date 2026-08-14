"""Full-response cache: canonical-query -> serialized ParseResponse dict.

A swappable-interface seam — an in-memory TTL cache today, a Redis-backed
implementation later, with no change to parse_service.
"""

import hashlib
from abc import ABC, abstractmethod
from typing import Any

from cachetools import TTLCache

from config import settings
from logger import log_event
from metrics import PARSE_CACHE_RESULT_TOTAL
from repositories.taxonomy_repository import taxonomy_repository


def build_cache_key(canonical_query: str) -> str:
    """Key = hash(taxonomy_version + canonical_query). Editing
    data/taxonomy.json changes taxonomy_version, which invalidates every
    stale entry for free — no manual cache-clear needed on a taxonomy update.
    """
    payload = f"{taxonomy_repository.taxonomy_version}:{canonical_query}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CacheRepository(ABC):
    @abstractmethod
    def get(self, key: str) -> dict[str, Any] | None:
        """Return the cached value for ``key``, or None on a miss/expiry."""

    @abstractmethod
    def set(self, key: str, value: dict[str, Any]) -> None:
        """Store ``value`` under ``key``, subject to the cache's TTL/size limits."""


class InMemoryTTLCache(CacheRepository):
    """Process-local cache backed by ``cachetools.TTLCache`` — bounded size
    with least-recently-used eviction once full, and time-based expiry.
    Lost on process restart — an accepted trade-off for local development
    and demos.
    """

    def __init__(self) -> None:
        self._cache: TTLCache = TTLCache(maxsize=settings.cache_max_size, ttl=settings.cache_ttl_seconds)

    def get(self, key: str) -> dict[str, Any] | None:
        value = self._cache.get(key)
        result = "hit" if value is not None else "miss"
        log_event(event="cache_lookup", result=result)
        PARSE_CACHE_RESULT_TOTAL.labels(result=result).inc()
        return value

    def set(self, key: str, value: dict[str, Any]) -> None:
        self._cache[key] = value


cache_repository: CacheRepository = InMemoryTTLCache()
