import sqlite3
import sys
import time
from pathlib import Path

import pytest

from similar_files import Cache, CacheError, default_cache_dir, resolve_cache_path
from similar_files.cache import SCHEMA_VERSION, FeatureKey

KEY = FeatureKey("fake", 1, "d1")


def test_default_location_per_platform(monkeypatch, tmp_path):
    d = default_cache_dir()
    if sys.platform == "darwin":
        assert d == Path.home() / "Library" / "Caches" / "similar-files"
    elif sys.platform.startswith("linux"):
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
        assert default_cache_dir() == tmp_path / "similar-files"


def test_env_var_and_argument_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SIMILAR_FILES_CACHE", str(tmp_path / "env.db"))
    assert resolve_cache_path() == tmp_path / "env.db"
    assert resolve_cache_path(tmp_path / "arg.db") == tmp_path / "arg.db"
    assert resolve_cache_path(tmp_path) == tmp_path / "cache.sqlite3"  # an existing directory
    monkeypatch.delenv("SIMILAR_FILES_CACHE")
    assert resolve_cache_path() == default_cache_dir() / "cache.sqlite3"


def test_wal_mode(cache_path):
    with Cache(cache_path):
        pass
    conn = sqlite3.connect(cache_path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_hash_matches_only_on_same_stat(cache_path):
    with Cache(cache_path) as c:
        c.put_hashes([("/a", 10, 100, None, "sha256:x")])
        assert c.content_hash("/a", 10, 100) == "sha256:x"
        assert c.content_hash("/a", 11, 100) is None
        assert c.content_hash("/a", 10, 101) is None
        assert c.content_hash("/a", 10, 100, "etag") is None


def test_undecodable_filenames_are_stored_exactly(cache_path):
    uri = "/tmp/caf\udce9.jpg"  # os.fsdecode of a Latin-1 byte
    with Cache(cache_path) as c:
        c.put_hashes([(uri, 1, 1, None, "sha256:y")])
        assert c.content_hash(uri, 1, 1) == "sha256:y"


def test_features_by_file_stat_and_key(cache_path):
    with Cache(cache_path) as c:
        c.put_features([("/x", 10, 100, None, KEY, b"\x01"), ("/bad", 1, 1, "e1", KEY, None)])
        assert c.feature("/x", 10, 100, None, KEY) == (True, b"\x01")
        assert c.feature("/bad", 1, 1, "e1", KEY) == (True, None)
        # Another stat, another version or other parameters: not found.
        assert c.feature("/x", 11, 100, None, KEY) == (False, None)
        assert c.feature("/x", 10, 101, None, KEY) == (False, None)
        assert c.feature("/bad", 1, 1, "e2", KEY) == (False, None)
        assert c.feature("/x", 10, 100, None, FeatureKey("fake", 2, "d1")) == (False, None)
        assert c.feature("/x", 10, 100, None, FeatureKey("fake", 1, "d2")) == (False, None)
        # A changed file's new feature replaces the old row.
        c.put_features([("/x", 11, 100, None, KEY, b"\x02")])
        assert c.feature("/x", 11, 100, None, KEY) == (True, b"\x02")
        assert c.stats()["features"] == 2


def test_gc_by_age_and_size(cache_path):
    with Cache(cache_path) as c:
        c.put_hashes([("/old", 1, 1, None, "sha256:o"), ("/new", 1, 1, None, "sha256:n")])
        c.put_features([(f"/{i}", 1, 1, None, KEY, b"x" * 1000) for i in range(10)])
        old = int(time.time()) - 100 * 86400
        c._conn.execute("UPDATE files SET last_seen=? WHERE uri=?", (old, b"/old"))
        c._conn.execute("UPDATE features SET last_seen=? WHERE uri=?", (old, b"/0"))
        r = c.gc(max_age_days=30)
        assert (r.files_removed, r.features_removed) == (1, 1)
        assert c.content_hash("/new", 1, 1) == "sha256:n"
        r = c.gc(max_bytes=5000)
        assert c.stats()["features"] <= 4


def test_schema_1_keeps_its_hashes_and_drops_its_features(cache_path):
    cache_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(cache_path)
    conn.executescript("""
        CREATE TABLE files (uri BLOB PRIMARY KEY, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
                            validator TEXT, content_hash TEXT NOT NULL, last_seen INTEGER NOT NULL);
        CREATE TABLE features (content_hash TEXT NOT NULL, extractor TEXT NOT NULL,
                               extractor_version INTEGER NOT NULL, params_digest TEXT NOT NULL, data BLOB,
                               last_seen INTEGER NOT NULL);
        INSERT INTO files VALUES (X'2F61', 10, 100, NULL, 'sha256:x', 0);
        INSERT INTO features VALUES ('sha256:x', 'fake', 1, 'd1', X'01', 0);
        PRAGMA user_version = 1;
    """)
    conn.close()
    with Cache(cache_path) as c:
        assert c.content_hash("/a", 10, 100) == "sha256:x"
        assert c.stats()["features"] == 0
        c.put_features([("/a", 10, 100, None, KEY, b"\x01")])
        assert c.feature("/a", 10, 100, None, KEY) == (True, b"\x01")


def test_newer_schema_is_refused(cache_path):
    cache_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(cache_path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(CacheError):
        Cache(cache_path)


def test_foreign_database_is_not_touched(cache_path):
    cache_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(cache_path)
    conn.execute("CREATE TABLE precious (x)")
    conn.commit()
    conn.close()
    with pytest.raises(CacheError):
        Cache(cache_path)
    conn = sqlite3.connect(cache_path)
    assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("precious",)]
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_not_a_database_is_refused(cache_path):
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(b"not sqlite at all, just some text" * 10)
    with pytest.raises(CacheError):
        Cache(cache_path)


def test_many_openers_of_a_new_cache_at_once(tmp_path):
    # Separate processes in real life; separate connections are enough to race.
    import threading

    path = tmp_path / "race.sqlite3"
    errors = []

    def open_and_write(n):
        try:
            with Cache(path) as c:
                c.put_hashes([(f"/f{n}", 1, 1, None, "sha256:x")])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=open_and_write, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with Cache(path) as c:
        assert c.stats()["files"] == 8
