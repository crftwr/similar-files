import os

import pytest


@pytest.fixture
def cache_path(tmp_path):
    return tmp_path / "cache" / "cache.sqlite3"


@pytest.fixture(autouse=True)
def _no_user_cache(tmp_path, monkeypatch):
    # A test that forgets to pass a cache must never touch the real per-user one.
    monkeypatch.setenv("SIMILAR_FILES_CACHE", str(tmp_path / "default-cache.sqlite3"))


def write(path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def random_bytes(n: int, seed: int) -> bytes:
    import random

    return random.Random(seed).randbytes(n)
