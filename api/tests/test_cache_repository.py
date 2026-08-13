import time

from app.repositories.cache_repository import InMemoryTTLCache, build_cache_key
from app.repositories.taxonomy_repository import taxonomy_repository


def test_miss_then_hit():
    cache = InMemoryTTLCache()
    key = build_cache_key("דירה בירושלים")
    assert cache.get(key) is None
    cache.set(key, {"category": "נדל״ן"})
    assert cache.get(key) == {"category": "נדל״ן"}


def test_cache_key_changes_when_taxonomy_version_changes(monkeypatch):
    key_before = build_cache_key("דירה בירושלים")
    monkeypatch.setattr(taxonomy_repository, "taxonomy_version", "different-version")
    key_after = build_cache_key("דירה בירושלים")
    assert key_before != key_after


def test_cache_key_is_stable_for_the_same_query_and_taxonomy_version():
    assert build_cache_key("דירה בירושלים") == build_cache_key("דירה בירושלים")


def test_cache_key_differs_for_different_queries():
    assert build_cache_key("דירה בירושלים") != build_cache_key("רכב בתל אביב")


def test_entry_expires_after_ttl(monkeypatch):
    from app import config as config_module

    monkeypatch.setattr(config_module.settings, "cache_ttl_seconds", 1)
    monkeypatch.setattr(config_module.settings, "cache_max_size", 100)
    cache = InMemoryTTLCache()
    key = build_cache_key("דירה בירושלים")
    cache.set(key, {"category": "נדל״ן"})
    assert cache.get(key) is not None
    time.sleep(1.2)
    assert cache.get(key) is None
