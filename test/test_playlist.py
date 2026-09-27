import os

from similar_files import Group, LocalFile, Member, read_playlist, write_playlists
from similar_files.playlist import playlist_name, safe_name


def item(path, size=1):
    return LocalFile(path=path, size=size, mtime_ns=0)


def group(anchor, *members, method="identical"):
    return Group(item(anchor), [Member(item(p), s) for p, s in members], method)


def test_anchor_first_then_members(tmp_path):
    g = group("/photos/IMG_0012.jpg", ("/b/copy.jpg", 0.97), ("/c/other.jpg", 0.9), method="image")
    (path,) = write_playlists([g], tmp_path)
    assert path.name == "IMG_0012.jpg (2).m3u8"
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines[0] == "#EXTM3U"
    assert [l for l in lines if not l.startswith("#")] == ["/photos/IMG_0012.jpg", "/b/copy.jpg", "/c/other.jpg"]
    header, entries = read_playlist(path)
    assert header["method"] == "image" and header["members"] == 2 and header["format"] == 1
    assert entries[0][1]["role"] == "anchor"
    assert entries[1] == ("/b/copy.jpg", {"role": "member", "similarity": 0.97})


def test_collisions_get_unique_names(tmp_path):
    (tmp_path / "a.jpg (1).m3u8").write_text("not ours")
    gs = [group("/x/a.jpg", ("/y/b", 1.0)), group("/z/A.JPG", ("/y/c", 1.0)), group("/w/a.jpg", ("/y/d", 1.0))]
    names = sorted(p.name for p in write_playlists(gs, tmp_path))
    assert names == ["A.JPG (1) [3].m3u8", "a.jpg (1) [2].m3u8", "a.jpg (1) [4].m3u8"]
    assert (tmp_path / "a.jpg (1).m3u8").read_text() == "not ours"


def test_names_are_safe_everywhere():
    assert safe_name('a<b>c:d"e|f?g*h') == "a_b_c_d_e_f_g_h"
    assert safe_name("CON.txt") == "_CON.txt"
    assert safe_name("trailing. ") == "trailing"
    assert len(safe_name("é" * 300).encode()) <= 200
    assert playlist_name(group("s3://bucket/dir/photo.jpg", ("s3://bucket/x.jpg", 1.0))) == "photo.jpg (1)"
    assert playlist_name(group("C:\\Users\\me\\pic.png", ("C:\\x.png", 1.0))) == "pic.png (1)"


def test_remote_uris_are_written_as_is(tmp_path):
    g = group("ssh://host/home/me/a.jpg", ("ssh://host/home/me/b.jpg", 1.0))
    (path,) = write_playlists([g], tmp_path)
    _, entries = read_playlist(path)
    assert [u for u, _ in entries] == ["ssh://host/home/me/a.jpg", "ssh://host/home/me/b.jpg"]


def test_undecodable_names_round_trip(tmp_path):
    uri = os.fsdecode(b"/photos/caf\xe9.jpg")
    (path,) = write_playlists([group(uri, ("/b.jpg", 1.0))], tmp_path)
    assert b"/photos/caf\xe9.jpg\n" in path.read_bytes()


def test_line_breaks_in_names_are_left_out(tmp_path):
    g = group("/a.jpg", ("/bad\nname.jpg", 1.0), ("/good.jpg", 1.0))
    (path,) = write_playlists([g], tmp_path)
    _, entries = read_playlist(path)
    assert [u for u, _ in entries] == ["/a.jpg", "/good.jpg"]
    assert write_playlists([group("/bad\n.jpg", ("/x", 1.0))], tmp_path / "other") == []


def test_replace_removes_only_our_earlier_playlists(tmp_path):
    from similar_files import our_playlists, remove_playlists

    write_playlists([group("/x/a.jpg", ("/y/b", 1.0))], tmp_path)
    (tmp_path / "mine.m3u8").write_text("#EXTM3U\n/some/song.mp3\n")
    (tmp_path / "notes.txt").write_text("keep")
    (written,) = write_playlists([group("/x/c.jpg", ("/y/d", 1.0))], tmp_path, replace=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["c.jpg (1).m3u8", "mine.m3u8", "notes.txt"]
    assert our_playlists(tmp_path) == [written]
    assert remove_playlists(tmp_path) == 1
    assert our_playlists(tmp_path / "missing") == []
