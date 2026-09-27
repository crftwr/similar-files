import io
import os
from dataclasses import dataclass
from typing import Optional

from similar_files import Cache, LocalFile, find_identical, walk
from similar_files.identical import SAMPLE_BYTES

from conftest import random_bytes, write


def names(group):
    return [os.path.basename(i.uri) for i in group.items]


def test_groups_identical_files(tmp_path, cache_path):
    data = random_bytes(1000, 1)
    write(tmp_path / "a" / "one.bin", data)
    write(tmp_path / "b" / "two.bin", data)
    write(tmp_path / "c" / "three.bin", data)
    write(tmp_path / "other.bin", random_bytes(1000, 2))  # same size, different bytes
    write(tmp_path / "lonely.bin", random_bytes(999, 3))
    result = find_identical(walk([tmp_path]), cache=cache_path)
    assert len(result.groups) == 1
    g = result.groups[0]
    assert sorted(names(g)) == ["one.bin", "three.bin", "two.bin"]
    assert all(m.similarity == 1.0 for m in g.members)
    assert not result.cancelled and result.errors == 0


def test_empty_files_are_ignored_by_default(tmp_path, cache_path):
    write(tmp_path / "a", b"")
    write(tmp_path / "b", b"")
    assert find_identical(walk([tmp_path]), cache=cache_path).groups == []
    assert len(find_identical(walk([tmp_path]), cache=cache_path, min_size=0).groups) == 1


def test_same_head_and_tail_but_different_middle(tmp_path, cache_path):
    size = SAMPLE_BYTES * 4
    base = bytearray(random_bytes(size, 4))
    write(tmp_path / "a.bin", bytes(base))
    base[size // 2] ^= 0xFF
    write(tmp_path / "b.bin", bytes(base))
    write(tmp_path / "c.bin", bytes(base))
    result = find_identical(walk([tmp_path]), cache=cache_path)
    assert [sorted(names(g)) for g in result.groups] == [["b.bin", "c.bin"]]


def test_largest_groups_come_first(tmp_path, cache_path):
    small, big = random_bytes(100, 5), random_bytes(10000, 6)
    for name in ("s1", "s2"):
        write(tmp_path / name, small)
    for name in ("b1", "b2"):
        write(tmp_path / name, big)
    streamed = []
    result = find_identical(walk([tmp_path]), cache=cache_path, on_group=streamed.append)
    assert [g.anchor.size for g in result.groups] == [10000, 100]
    assert streamed == result.groups


def test_anchor_is_the_oldest_file(tmp_path, cache_path):
    data = random_bytes(500, 7)
    new = write(tmp_path / "a-new.bin", data)
    old = write(tmp_path / "z-old.bin", data)
    os.utime(old, ns=(1_000_000_000, 1_000_000_000))
    os.utime(new, ns=(2_000_000_000, 2_000_000_000))
    (g,) = find_identical(walk([tmp_path]), cache=cache_path).groups
    assert os.path.basename(g.anchor.uri) == "z-old.bin"


def test_hard_links_are_not_duplicates(tmp_path, cache_path):
    write(tmp_path / "a.bin", random_bytes(500, 8))
    os.link(tmp_path / "a.bin", tmp_path / "b.bin")
    assert find_identical(walk([tmp_path]), cache=cache_path).groups == []


@dataclass(eq=False)
class CountingFile:
    """An item that counts how often its content is opened."""

    uri: str
    data: bytes
    mtime_ns: int = 1
    validator: Optional[str] = None
    is_remote: bool = False
    provided: Optional[str] = None
    opened: int = 0

    @property
    def size(self):
        return len(self.data)

    def open(self):
        self.opened += 1
        return io.BytesIO(self.data)

    def content_hash(self):
        return self.provided


def test_second_scan_reads_nothing(cache_path):
    data = random_bytes(SAMPLE_BYTES * 3, 9)
    items = [CountingFile("mem://a", data), CountingFile("mem://b", data)]
    assert len(find_identical(items, cache=cache_path).groups) == 1
    assert all(i.opened > 0 for i in items)
    for i in items:
        i.opened = 0
    assert len(find_identical(items, cache=cache_path).groups) == 1
    assert all(i.opened == 0 for i in items)


def test_changed_mtime_invalidates_the_cached_hash(cache_path):
    data = random_bytes(3000, 10)
    items = [CountingFile("mem://a", data), CountingFile("mem://b", data)]
    find_identical(items, cache=cache_path)
    items[1] = CountingFile("mem://b", random_bytes(3000, 11), mtime_ns=2)
    assert find_identical(items, cache=cache_path).groups == []
    assert items[1].opened > 0


def test_provided_hashes_avoid_reading(cache_path):
    data = random_bytes(3000, 12)
    a = CountingFile("s3://b/a", data, is_remote=True, provided="md5:abc")
    b = CountingFile("s3://b/b", data, is_remote=True, provided="md5:abc")
    c = CountingFile("s3://b/c", random_bytes(3000, 13), is_remote=True, provided="md5:def")
    result = find_identical([a, b, c], cache=False)
    assert [sorted(i.uri for i in g.items) for g in result.groups] == [["s3://b/a", "s3://b/b"]]
    assert a.opened == b.opened == c.opened == 0


def test_mixed_hash_algorithms_fall_back_to_reading(cache_path):
    data = random_bytes(3000, 14)
    a = CountingFile("s3://b/a", data, provided="md5:abc")
    b = CountingFile("/local/b", data)
    result = find_identical([a, b], cache=False)
    assert len(result.groups) == 1


def test_reference_mode(tmp_path, cache_path):
    data = random_bytes(800, 15)
    ref = write(tmp_path / "ref" / "original.bin", data)
    write(tmp_path / "scan" / "copy1.bin", data)
    write(tmp_path / "scan" / "copy2.bin", data)
    write(tmp_path / "scan" / "unrelated.bin", random_bytes(800, 16))
    result = find_identical(
        walk([tmp_path]),  # the reference is inside the scanned tree too
        references=[LocalFile.from_path(ref)],
        cache=cache_path,
    )
    (g,) = result.groups
    assert os.path.basename(g.anchor.uri) == "original.bin"
    assert names(g)[1:] == ["copy1.bin", "copy2.bin"]


def test_references_are_never_members_of_each_other(tmp_path, cache_path):
    data = random_bytes(800, 17)
    r1 = write(tmp_path / "r1.bin", data)
    r2 = write(tmp_path / "r2.bin", data)
    write(tmp_path / "scan" / "c.bin", data)
    result = find_identical(
        walk([tmp_path / "scan"]),
        references=[LocalFile.from_path(r1), LocalFile.from_path(r2)],
        cache=cache_path,
    )
    assert sorted(names(g) for g in result.groups) == [["r1.bin", "c.bin"], ["r2.bin", "c.bin"]]


class Broken(CountingFile):
    def open(self):
        raise PermissionError("denied")


def test_unreadable_file_is_counted_not_fatal(cache_path):
    data = random_bytes(3000, 18)
    items = [CountingFile("mem://a", data), CountingFile("mem://b", data), Broken("mem://c", data)]
    result = find_identical(items, cache=cache_path)
    assert len(result.groups) == 1
    assert result.errors == 1 and result.error_uris == ["mem://c"]


def test_cancel_keeps_the_cache_valid(cache_path):
    data = random_bytes(3000, 19)
    items = [CountingFile("mem://a", data), CountingFile("mem://b", data)]
    result = find_identical(items, cache=cache_path, cancel=lambda: True)
    assert result.cancelled
    result = find_identical(items, cache=cache_path)
    assert len(result.groups) == 1


def test_cache_object_is_left_open_for_the_caller(cache_path):
    with Cache(cache_path) as cache:
        find_identical([], cache=cache)
        assert cache.stats()["files"] == 0


def test_cancel_keeps_hashes_finished_in_later_buckets(tmp_path, cache_path, monkeypatch):
    """The largest bucket is waited for first; hashes already done for smaller ones are still cached."""
    import time

    from similar_files import identical
    from similar_files.model import Cancelled

    real_hash = identical.hash_stream

    def slow_for_big(stream, stop=None):
        if getattr(stream, "name", "").endswith("big1"):
            while not stop.is_set():
                time.sleep(0.01)
            raise Cancelled()
        return real_hash(stream, stop)

    monkeypatch.setattr(identical, "hash_stream", slow_for_big)
    for name in ("big1", "big2"):
        write(tmp_path / name, b"B" * 100)
    for name in ("small1", "small2"):
        write(tmp_path / name, b"s" * 50)
    started = time.monotonic()
    result = find_identical(walk([tmp_path]), cache=cache_path, cancel=lambda: time.monotonic() - started > 1)
    assert result.cancelled
    with Cache(cache_path) as c:
        for name in ("small1", "small2", "big2"):
            item = LocalFile.from_path(tmp_path / name)
            assert c.content_hash(item.uri, item.size, item.mtime_ns) is not None, name
