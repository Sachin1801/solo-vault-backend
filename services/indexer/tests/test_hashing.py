"""Unit tests for app.cache.hashing — no infrastructure required."""

import hashlib
import tempfile
from pathlib import Path

import pytest

from app.cache.hashing import chunk_hash, file_hash


# ── file_hash ─────────────────────────────────────────────────────────────────


def _write(content: bytes, suffix: str = ".bin") -> str:
    f = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    f.write(content)
    f.close()
    return f.name


def test_file_hash_returns_hex_string():
    path = _write(b"hello")
    result = file_hash(path)
    assert isinstance(result, str)
    assert len(result) == 64
    assert all(c in "0123456789abcdef" for c in result)


def test_file_hash_deterministic():
    path = _write(b"same content")
    assert file_hash(path) == file_hash(path)


def test_file_hash_matches_sha256():
    data = b"test data for hashing"
    expected = hashlib.sha256(data).hexdigest()
    path = _write(data)
    assert file_hash(path) == expected


def test_file_hash_differs_for_different_content():
    a = _write(b"aaa")
    b = _write(b"bbb")
    assert file_hash(a) != file_hash(b)


def test_file_hash_empty_file():
    path = _write(b"")
    result = file_hash(path)
    assert result == hashlib.sha256(b"").hexdigest()


def test_file_hash_large_file():
    # 3 MB — triggers multi-chunk read path
    data = b"x" * (3 * 1024 * 1024)
    expected = hashlib.sha256(data).hexdigest()
    path = _write(data)
    assert file_hash(path) == expected


# ── chunk_hash ────────────────────────────────────────────────────────────────


def test_chunk_hash_returns_hex_string():
    result = chunk_hash("hello world")
    assert isinstance(result, str)
    assert len(result) == 64


def test_chunk_hash_deterministic():
    assert chunk_hash("repeat") == chunk_hash("repeat")


def test_chunk_hash_matches_sha256():
    text = "some chunk text"
    expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert chunk_hash(text) == expected


def test_chunk_hash_differs_for_different_content():
    assert chunk_hash("aaa") != chunk_hash("bbb")


def test_chunk_hash_unicode():
    text = "Привет мир 🌍"
    expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert chunk_hash(text) == expected


def test_chunk_hash_empty_string():
    result = chunk_hash("")
    assert result == hashlib.sha256(b"").hexdigest()
