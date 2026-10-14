"""The .xft tree output: snapshots, names, and a stopped scan's last write."""

from similar_files.model import Group, Member
from similar_files.tree import TreeWriter, read, render


class _Item:
    def __init__(self, uri, size=10, mtime_ns=1_000_000_000):
        self.uri, self.size, self.mtime_ns = uri, size, mtime_ns


def _group(anchor, *members):
    return Group(_Item(anchor), [Member(_Item(m), 0.9) for m in members], "video", 1, {}, 0.8)


def test_same_names_are_told_apart():
    doc = render([_group("/a/x.mp4", "/b/x.mp4"), _group("/c/x.mp4", "/d/y.mp4")],
                 title="t", final=True, generation=1)
    assert [n["name"] for n in doc["nodes"]] == ["x.mp4 (1)", "x.mp4 (1) [2]"]
    assert [c["name"] for c in doc["nodes"][0]["children"]] == ["x.mp4", "x.mp4 [2]"]


def test_snapshots_count_generations_and_stay_provisional(tmp_path):
    path = tmp_path / "r.xft"
    w = TreeWriter(path, title="t")
    w.snapshot([_group("/a.mp4", "/b.mp4")], done=3, total=10)
    doc = read(path)
    assert (doc["final"], doc["complete"], doc["generation"]) == (False, False, 1)
    assert doc["progress"] == {"done": 3, "total": 10}
    w.snapshot([_group("/a.mp4", "/b.mp4", "/c.mp4")], done=6, total=10)
    assert read(path)["generation"] == 2
    w.finish([_group("/a.mp4", "/b.mp4", "/c.mp4")], done=10, total=10)
    doc = read(path)
    assert (doc["final"], doc["complete"], doc["generation"]) == (True, True, 3)
    assert not (tmp_path / "r.xft.tmp").exists()


def test_a_stopped_scan_keeps_its_last_snapshot(tmp_path):
    path = tmp_path / "r.xft"
    w = TreeWriter(path, title="t")
    w.snapshot([_group("/a.mp4", "/b.mp4")], done=4, total=10)
    w.finish([], complete=False)
    doc = read(path)
    assert doc["final"] is True and doc["complete"] is False
    assert len(doc["nodes"]) == 1
    assert doc["progress"] == {"done": 4, "total": 10}


def test_final_groups_are_marked_while_the_scan_runs():
    g1, g2 = _group("/a", "/b"), _group("/c", "/d")
    doc = render([g1, g2], title="t", final=False, generation=1, final_groups={id(g1)})
    assert doc["nodes"][0].get("final") is True
    assert "final" not in doc["nodes"][1]


def test_read_rejects_other_files(tmp_path):
    (tmp_path / "x.json").write_text('{"format": "other", "version": 1}')
    assert read(tmp_path / "x.json") is None
