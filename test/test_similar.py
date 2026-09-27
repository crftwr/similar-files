"""Star grouping and the extraction pipeline, with a fake extractor (no optional dependencies)."""

import io
import os

import pytest

from similar_files import (
    ExtractionFailed,
    Extractor,
    FeatureSet,
    LocalFile,
    extract_features,
    find_similar,
    group_features,
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


def test_features_are_cached_and_reused_by_content(tmp_path, cache_path):
    make(tmp_path, a=1, b=2)
    extract_features(walk([tmp_path]), NumberExtractor(), cache=cache_path)
    assert NumberExtractor.calls == 2
    # Unchanged files: no extraction. A copy under a new name: found by content hash.
    write(tmp_path / "copy.num", b"1")
    extract_features(walk([tmp_path]), NumberExtractor(), cache=cache_path)
    assert NumberExtractor.calls == 2


def test_parameters_are_part_of_the_cache_key(tmp_path, cache_path):
    make(tmp_path, a=1)
    extract_features(walk([tmp_path]), NumberExtractor(), cache=cache_path)
    extract_features(walk([tmp_path]), NumberExtractor(scale=20), cache=cache_path)
    assert NumberExtractor.calls == 2
    extract_features(walk([tmp_path]), NumberExtractor(), cache=cache_path)
    assert NumberExtractor.calls == 2  # switching back is free


def test_duplicates_are_extracted_once(tmp_path):
    make(tmp_path, a=5, b=5, c=5)
    extract_features(walk([tmp_path]), NumberExtractor(), cache=False, workers=1)
    assert NumberExtractor.calls == 1


def test_unreadable_content_is_cached_as_a_failure(tmp_path, cache_path):
    write(tmp_path / "bad.num", b"hello")
    fs = extract_features(walk([tmp_path]), NumberExtractor(), cache=cache_path)
    assert fs.result.unreadable == 1 and fs.items == []
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
