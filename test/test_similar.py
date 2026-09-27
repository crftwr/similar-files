"""Star grouping and the extraction pipeline, with a fake extractor (no optional dependencies)."""

import io
import os
import sys
import time

import pytest

from similar_files import (
    ExtractionFailed,
    Extractor,
    FeatureSet,
    LocalFile,
    extract_features,
    find_similar,
    group_features,
    run_program,
    walk,
)

from conftest import write


class NumberExtractor(Extractor):
    """A file holding a number; similarity falls off linearly with the difference."""

    name = "test-number"
    version = 1
    extensions = frozenset({".num"})
    default_params = {"scale": 10}
    default_threshold = 0.5
    calls = 0

    def extract(self, stream):
        type(self).calls += 1
        text = stream.read().decode()
        if not text.strip().lstrip("-").isdigit():
            raise ExtractionFailed("not a number")
        return int(text).to_bytes(8, "big", signed=True)

    def similarity(self, a, b):
        d = abs(int.from_bytes(a, "big", signed=True) - int.from_bytes(b, "big", signed=True))
        return max(0.0, 1.0 - d / self.params["scale"])


@pytest.fixture(autouse=True)
def reset_calls():
    NumberExtractor.calls = 0


def make(tmp_path, **numbers):
    for name, value in numbers.items():
        write(tmp_path / f"{name}.num", str(value).encode())


def base(g):
    return [os.path.splitext(os.path.basename(i.uri))[0] for i in g.items]


def test_grouping_is_star_shaped_not_transitive(tmp_path, cache_path):
    # a≈b (0.6) and b≈c (0.6), but a and c are unrelated (0.2).
    make(tmp_path, a=0, b=4, c=8)
    # Make b the largest file so it is picked as the anchor first.
    write(tmp_path / "b.num", b"4" + b" " * 10)
    result = find_similar(walk([tmp_path]), NumberExtractor(), cache=cache_path)
    assert [base(g) for g in result.groups] == [["b", "a", "c"]]
    # With a the anchor, c is not pulled in through b.
    write(tmp_path / "a.num", b"0" + b" " * 20)
    result = find_similar(walk([tmp_path]), NumberExtractor(), cache=cache_path)
    assert base(result.groups[0]) == ["a", "b"]
    assert all(m.similarity >= 0.5 for g in result.groups for m in g.members)


def test_members_are_sorted_nearest_first_with_similarity(tmp_path, cache_path):
    make(tmp_path, a=0, b=3, c=1)
    write(tmp_path / "a.num", b"0      ")
    (g,) = find_similar(walk([tmp_path]), NumberExtractor(), cache=cache_path).groups
    assert base(g) == ["a", "c", "b"]
    assert [m.similarity for m in g.members] == [pytest.approx(0.9), pytest.approx(0.7)]


def test_regroup_without_reextracting(tmp_path, cache_path):
    make(tmp_path, a=0, b=2, c=6)
    fs = extract_features(walk([tmp_path]), NumberExtractor(), cache=cache_path)
    calls = NumberExtractor.calls
    loose = group_features(fs, 0.3)
    tight = group_features(fs, 0.9)
    assert NumberExtractor.calls == calls
    assert sum(len(g.members) for g in loose) > 0
    assert tight == []


def test_features_are_cached_by_file(tmp_path, cache_path):
    make(tmp_path, a=1, b=2)
    extract_features(walk([tmp_path]), NumberExtractor(), cache=cache_path)
    assert NumberExtractor.calls == 2
    # Unchanged files: no extraction. A new file (even a copy) and a changed one: extracted.
    write(tmp_path / "copy.num", b"1")
    write(tmp_path / "b.num", b"3")
    os.utime(tmp_path / "b.num", ns=(1, 1))
    fs = extract_features(walk([tmp_path]), NumberExtractor(), cache=cache_path)
    assert NumberExtractor.calls == 4
    assert sorted(int.from_bytes(f, "big") for f in fs.features) == [1, 1, 3]


def test_parameters_are_part_of_the_cache_key(tmp_path, cache_path):
    make(tmp_path, a=1)
    extract_features(walk([tmp_path]), NumberExtractor(), cache=cache_path)
    extract_features(walk([tmp_path]), NumberExtractor(scale=20), cache=cache_path)
    assert NumberExtractor.calls == 2
    extract_features(walk([tmp_path]), NumberExtractor(), cache=cache_path)
    assert NumberExtractor.calls == 2  # switching back is free


def test_unreadable_content_is_cached_as_a_failure(tmp_path, cache_path):
    write(tmp_path / "bad.num", b"hello")
    fs = extract_features(walk([tmp_path]), NumberExtractor(), cache=cache_path)
    assert fs.result.unreadable == 1 and fs.items == []
    extract_features(walk([tmp_path]), NumberExtractor(), cache=cache_path)
    assert NumberExtractor.calls == 1


def test_transient_failure_is_not_cached(tmp_path, cache_path):
    class Flaky(NumberExtractor):
        def extract(self, stream):
            type(self).calls += 1
            raise ExtractionFailed("took too long", transient=True)

    make(tmp_path, a=1)
    fs = extract_features(walk([tmp_path]), Flaky(), cache=cache_path)
    assert fs.result.errors == 1 and fs.result.unreadable == 0
    extract_features(walk([tmp_path]), Flaky(), cache=cache_path)
    assert Flaky.calls == 2


def test_cancel_kills_a_running_program_and_caches_nothing(tmp_path, cache_path):
    class Slow(NumberExtractor):
        needs_path = True

        def extract_file(self, path):
            type(self).calls += 1
            run_program([sys.executable, "-c", "import time; time.sleep(30)"], timeout=60)
            return b"never"

    make(tmp_path, a=1)
    started = time.monotonic()
    fs = extract_features(walk([tmp_path]), Slow(), cache=cache_path,
                          cancel=lambda: time.monotonic() - started > 0.5)
    assert fs.result.cancelled and time.monotonic() - started < 10
    assert Slow.calls == 1
    # Same feature key, and nothing was cached: the next scan extracts again.
    extract_features(walk([tmp_path]), NumberExtractor(), cache=cache_path)
    assert NumberExtractor.calls == 1


def test_only_handled_files_are_read(tmp_path, cache_path):
    make(tmp_path, a=1)
    write(tmp_path / "x.txt", b"1")
    fs = extract_features(walk([tmp_path]), NumberExtractor(), cache=cache_path)
    assert [os.path.basename(i.uri) for i in fs.items] == ["a.num"]


def test_remote_files_are_skipped_unless_asked(tmp_path, cache_path):
    make(tmp_path, a=1, b=1)
    items = list(walk([tmp_path], remote_roots=[tmp_path]))
    fs = extract_features(items, NumberExtractor(), cache=cache_path)
    assert fs.items == [] and fs.result.skipped_remote == 2
    fs = extract_features(items, NumberExtractor(), cache=cache_path, include_remote=True)
    assert len(fs.items) == 2


def test_reference_mode(tmp_path, cache_path):
    make(tmp_path / "scan", a=0, b=1, c=9)
    make(tmp_path / "refs", r1=0, r2=9)
    refs = [LocalFile.from_path(tmp_path / "refs" / n) for n in ("r1.num", "r2.num")]
    result = find_similar(walk([tmp_path]), NumberExtractor(), references=refs, cache=cache_path)
    assert [base(g) for g in result.groups] == [["r1", "a", "b"], ["r2", "c"]]


def test_threshold_is_validated(tmp_path):
    fs = FeatureSet(extractor=NumberExtractor())
    with pytest.raises(ValueError):
        group_features(fs, 1.5)


def test_cancel(tmp_path, cache_path):
    make(tmp_path, a=0, b=1)
    result = find_similar(walk([tmp_path]), NumberExtractor(), cache=cache_path, cancel=lambda: True)
    assert result.cancelled and result.groups == []


def test_a_file_is_read_only_by_the_program_that_extracts_it(tmp_path, cache_path, monkeypatch):
    """No content hash: a program-run extractor is the only reader, and a cached file is not read at all."""

    class PathNumber(NumberExtractor):
        needs_path = True

        def extract_file(self, path):
            type(self).calls += 1
            with open(path, "rb") as f:
                return int(f.read()).to_bytes(8, "big", signed=True)

    opened = []
    real_open = LocalFile.open
    monkeypatch.setattr(LocalFile, "open", lambda self: opened.append(self.uri) or real_open(self))
    make(tmp_path, a=1, b=2)
    fs = extract_features(walk([tmp_path]), PathNumber(), cache=cache_path)
    assert opened == [] and PathNumber.calls == 2
    assert sorted(int.from_bytes(f, "big") for f in fs.features) == [1, 2]
    extract_features(walk([tmp_path]), PathNumber(), cache=cache_path)
    assert opened == [] and PathNumber.calls == 2


def test_cancel_keeps_files_finished_behind_a_long_one(tmp_path, cache_path):
    """Results are taken as they finish: a long first file does not lose the ones done after it."""

    class SlowFirst(NumberExtractor):
        needs_path = True
        slow = True

        def extract_file(self, path):
            type(self).calls += 1
            if self.slow and os.path.basename(path) == "a.num":
                run_program([sys.executable, "-c", "import time; time.sleep(30)"], timeout=60)
            with open(path, "rb") as f:
                return int(f.read()).to_bytes(8, "big", signed=True)

    make(tmp_path, a=1, b=2, c=3, d=4)
    items = sorted(walk([tmp_path]), key=lambda i: i.uri)  # a.num is handed out first
    started = time.monotonic()
    fs = extract_features(items, SlowFirst(), cache=cache_path, workers=2,
                          cancel=lambda: SlowFirst.calls == 4 and time.monotonic() - started > 1)
    assert fs.result.cancelled and fs.result.errors == 0
    assert sorted(int.from_bytes(f, "big") for f in fs.features) == [2, 3, 4]
    # Only the file that was cut short is extracted again.
    SlowFirst.calls, SlowFirst.slow = 0, False
    fs = extract_features(items, SlowFirst(), cache=cache_path)
    assert SlowFirst.calls == 1 and len(fs.features) == 4


def test_each_file_is_reported(tmp_path, cache_path):
    make(tmp_path, a=1, b=2)
    write(tmp_path / "bad.num", b"hello")
    events = []

    def progress(p):
        if p.uri is not None:
            events.append((p.event, os.path.basename(p.uri)))

    extract_features(walk([tmp_path]), NumberExtractor(), cache=cache_path, progress=progress, workers=1)
    assert sorted(events) == sorted([("start", "a.num"), ("done", "a.num"), ("start", "b.num"), ("done", "b.num"),
                                     ("start", "bad.num"), ("unreadable", "bad.num")])
    events.clear()
    extract_features(walk([tmp_path]), NumberExtractor(), cache=cache_path, progress=progress)
    assert sorted(events) == [("cached", "a.num"), ("cached", "b.num"), ("cached", "bad.num")]
