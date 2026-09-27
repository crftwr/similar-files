import os

import pytest

from similar_files import WalkStats, walk

from conftest import write


def paths(items):
    return sorted(os.path.relpath(i.uri) for i in items)


def test_walk_yields_regular_files(tmp_path):
    write(tmp_path / "a.txt", b"a")
    write(tmp_path / "sub" / "b.txt", b"b")
    items = list(walk([tmp_path]))
    assert sorted(os.path.basename(i.uri) for i in items) == ["a.txt", "b.txt"]
    assert all(os.path.isabs(i.uri) for i in items)
    assert all(not i.is_remote for i in items)


def test_hard_links_are_one_file(tmp_path):
    write(tmp_path / "a.bin", b"x" * 10)
    os.link(tmp_path / "a.bin", tmp_path / "b.bin")
    stats = WalkStats()
    items = list(walk([tmp_path], stats=stats))
    assert len(items) == 1
    assert stats.hardlink_aliases == 1


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
def test_symlinks_not_followed_by_default(tmp_path):
    write(tmp_path / "real" / "a.bin", b"x")
    os.symlink(tmp_path / "real" / "a.bin", tmp_path / "file-link")
    os.symlink(tmp_path / "real", tmp_path / "dir-link")
    stats = WalkStats()
    items = list(walk([tmp_path], stats=stats))
    assert [os.path.basename(i.uri) for i in items] == ["a.bin"]
    assert stats.symlinks_skipped == 2


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlinks")
def test_followed_symlinks_visit_each_target_once_and_stop_at_cycles(tmp_path):
    write(tmp_path / "d" / "a.bin", b"x")
    os.symlink(tmp_path, tmp_path / "d" / "loop")  # a cycle back to the root
    os.symlink(tmp_path / "d" / "a.bin", tmp_path / "link-to-a")
    stats = WalkStats()
    items = list(walk([tmp_path], follow_symlinks=True, stats=stats))
    assert len(items) == 1
    assert stats.links_revisited == 1


def test_overlapping_roots_visit_each_file_once(tmp_path):
    write(tmp_path / "sub" / "a.bin", b"x")
    write(tmp_path / "b.bin", b"y")
    items = list(walk([tmp_path, tmp_path / "sub", tmp_path / "sub" / "a.bin"]))
    assert len(items) == 2


def test_remote_roots_mark_items(tmp_path):
    write(tmp_path / "net" / "a.bin", b"x")
    write(tmp_path / "local" / "b.bin", b"y")
    items = {os.path.basename(i.uri): i for i in walk([tmp_path], remote_roots=[tmp_path / "net"])}
    assert items["a.bin"].is_remote
    assert not items["b.bin"].is_remote


def test_missing_root_is_counted_not_raised(tmp_path):
    stats = WalkStats()
    assert list(walk([tmp_path / "nope"], stats=stats)) == []
    assert stats.errors == 1


def test_cancel_stops_walk(tmp_path):
    for i in range(5):
        write(tmp_path / f"d{i}" / "a.bin", b"x")
    assert list(walk([tmp_path], cancel=lambda: True)) == []
