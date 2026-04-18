"""Unit tests for app.cache — Redis is fully mocked, no infrastructure needed."""

import json
from unittest.mock import MagicMock, patch

import pytest

# We patch the module-level _redis singleton before importing cache functions
# so no real Redis connection is attempted.

_MOCK_REDIS = MagicMock()


@pytest.fixture(autouse=True)
def reset_mock():
    _MOCK_REDIS.reset_mock()
    with patch("app.cache.redis_cache._redis", _MOCK_REDIS):
        yield


from app.cache.hashing import chunk_hash  # noqa: E402
from app.cache.redis_cache import (  # noqa: E402
    cache_embedding,
    get_cached_embedding,
    is_file_indexed,
    mark_file_indexed,
)

# ── is_file_indexed ───────────────────────────────────────────────────────────


def test_is_file_indexed_true_when_key_exists():
    _MOCK_REDIS.exists.return_value = 1
    assert is_file_indexed("abc123") is True
    _MOCK_REDIS.exists.assert_called_once_with("cache:file:abc123")


def test_is_file_indexed_false_when_key_absent():
    _MOCK_REDIS.exists.return_value = 0
    assert is_file_indexed("nope") is False


def test_is_file_indexed_uses_correct_key_prefix():
    _MOCK_REDIS.exists.return_value = 0
    is_file_indexed("deadbeef")
    key = _MOCK_REDIS.exists.call_args[0][0]
    assert key.startswith("cache:file:")


# ── mark_file_indexed ─────────────────────────────────────────────────────────


def test_mark_file_indexed_calls_set_with_ttl():
    mark_file_indexed("hash1", "entry-1")
    _MOCK_REDIS.set.assert_called_once()
    args, kwargs = _MOCK_REDIS.set.call_args
    assert args[0] == "cache:file:hash1"
    assert args[1] == "entry-1"
    assert kwargs.get("ex") == 86400 * 7


def test_mark_file_indexed_custom_ttl():
    mark_file_indexed("hash2", "entry-2", ttl_seconds=3600)
    _, kwargs = _MOCK_REDIS.set.call_args
    assert kwargs.get("ex") == 3600


def test_mark_file_indexed_stores_entry_id():
    mark_file_indexed("h", "my-entry-id")
    args, _ = _MOCK_REDIS.set.call_args
    assert args[1] == "my-entry-id"


# ── get_cached_embedding ──────────────────────────────────────────────────────


def test_get_cached_embedding_returns_none_on_miss():
    _MOCK_REDIS.get.return_value = None
    result = get_cached_embedding("missing-hash")
    assert result is None


def test_get_cached_embedding_deserializes_json():
    vector = [0.1, 0.2, 0.3]
    _MOCK_REDIS.get.return_value = json.dumps(vector)
    result = get_cached_embedding("some-hash")
    assert result == vector


def test_get_cached_embedding_uses_correct_key():
    _MOCK_REDIS.get.return_value = None
    get_cached_embedding("myhash")
    _MOCK_REDIS.get.assert_called_once_with("cache:chunk:myhash")


def test_get_cached_embedding_returns_list_of_floats():
    vector = [float(i) / 10 for i in range(1024)]
    _MOCK_REDIS.get.return_value = json.dumps(vector)
    result = get_cached_embedding("h")
    assert isinstance(result, list)
    assert len(result) == 1024
    assert all(isinstance(v, float) for v in result)


# ── cache_embedding ───────────────────────────────────────────────────────────


def test_cache_embedding_serializes_to_json():
    vector = [1.0, 2.0, 3.0]
    cache_embedding("h", vector)
    args, kwargs = _MOCK_REDIS.set.call_args
    assert args[0] == "cache:chunk:h"
    assert json.loads(args[1]) == vector


def test_cache_embedding_default_ttl_30_days():
    cache_embedding("h", [0.1])
    _, kwargs = _MOCK_REDIS.set.call_args
    assert kwargs.get("ex") == 86400 * 30


def test_cache_embedding_custom_ttl():
    cache_embedding("h", [0.1], ttl_seconds=7200)
    _, kwargs = _MOCK_REDIS.set.call_args
    assert kwargs.get("ex") == 7200


# ── Round-trip: mark then check ───────────────────────────────────────────────


def test_file_cache_roundtrip():
    """Verify key written by mark matches key read by is_file_indexed."""
    fhash = "abc" * 20  # 60-char hash
    mark_file_indexed(fhash, "entry-99")
    written_key = _MOCK_REDIS.set.call_args[0][0]

    _MOCK_REDIS.exists.return_value = 1
    is_file_indexed(fhash)
    read_key = _MOCK_REDIS.exists.call_args[0][0]

    assert written_key == read_key


def test_chunk_cache_roundtrip():
    """Verify key written by cache_embedding matches key read by get_cached_embedding."""
    content = "some chunk text"
    h = chunk_hash(content)
    vector = [0.5] * 8

    cache_embedding(h, vector)
    written_key = _MOCK_REDIS.set.call_args[0][0]

    _MOCK_REDIS.get.return_value = json.dumps(vector)
    get_cached_embedding(h)
    read_key = _MOCK_REDIS.get.call_args[0][0]

    assert written_key == read_key
