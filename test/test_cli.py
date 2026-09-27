import os

import pytest

from similar_files.cli import main
from similar_files.playlist import read_playlist

from conftest import random_bytes, write


@pytest.fixture
def tree(tmp_path):
    data = random_bytes(2000, 1)
    write(tmp_path / "in" / "a.bin", data)
    write(tmp_path / "in" / "sub" / "b.bin", data)
    write(tmp_path / "in" / "c.bin", random_bytes(2000, 2))
    return tmp_path


def run(tree, *extra):
    return main(["scan", str(tree / "in"), "--out", str(tree / "out"), "--cache", str(tree / "c.db"), *extra])


def test_scan_writes_playlists(tree, capsys):
    assert run(tree) == 0
    (playlist,) = (tree / "out").iterdir()
    assert playlist.name == "a.bin (1).m3u8"
    _, entries = read_playlist(playlist)
    assert [os.path.basename(u) for u, _ in entries] == ["a.bin", "b.bin"]
    assert "1 group(s)" in capsys.readouterr().out


def test_rerun_needs_replace_and_replaces_only_our_playlists(tree):
    assert run(tree) == 0
    write(tree / "out" / "mine.m3u8", b"#EXTM3U\n/some/song.mp3\n")
    assert run(tree) == 2
    assert run(tree, "--replace") == 0
    assert sorted(p.name for p in (tree / "out").iterdir()) == ["a.bin (1).m3u8", "mine.m3u8"]


def test_reference_mode(tree):
    assert run(tree, "--reference", str(tree / "in" / "sub" / "b.bin")) == 0
    (playlist,) = (tree / "out").iterdir()
    _, entries = read_playlist(playlist)
    assert [os.path.basename(u) for u, _ in entries] == ["b.bin", "a.bin"]


def test_bad_options(tree):
    assert run(tree, "--method", "no-such") == 2
    assert run(tree, "--threshold", "0.5") == 2  # not for identical
    with pytest.raises(SystemExit):
        run(tree, "--method", "image", "--threshold", "1.5")


def test_image_scan(tmp_path):
    pytest.importorskip("PIL")
    pytest.importorskip("imagehash")
    from test_image import scene

    write(tmp_path / "in" / "x.txt", b"ignored")
    img = scene(7)
    img.save(tmp_path / "in" / "big.png")
    img.resize((160, 120)).save(tmp_path / "in" / "small.jpg")
    scene(8).save(tmp_path / "in" / "other.png")
    rc = main(["scan", str(tmp_path / "in"), "-o", str(tmp_path / "out"), "-m", "image", "-p", "hash_size=16",
               "--cache", str(tmp_path / "c.db")])
    assert rc == 0
    (playlist,) = (tmp_path / "out").iterdir()
    header, entries = read_playlist(playlist)
    assert header["method"] == "image" and header["params"]["hash_size"] == 16
    assert len(entries) == 2


def test_extractors_and_cache_commands(tree, capsys):
    assert main(["extractors"]) == 0
    assert "image" in capsys.readouterr().out
    run(tree)
    assert main(["cache", "--cache", str(tree / "c.db")]) == 0
    assert "file(s)" in capsys.readouterr().out
    assert main(["cache", "gc", "--cache", str(tree / "c.db"), "--max-age-days", "30"]) == 0


def test_progress_is_whole_lines_on_a_pipe(tree, capsys):
    # capsys is not a terminal, like XeFM's log pane reading a pipe.
    assert run(tree) == 0
    out = capsys.readouterr().out
    assert "\r" not in out
    lines = out.splitlines()
    assert any(line.startswith(("sample:", "hash:")) for line in lines)
    assert lines[-1].startswith("1 group(s)")


def test_each_file_gets_a_line_and_a_rerun_says_cached(tree, capsys):
    assert run(tree) == 0
    lines = capsys.readouterr().out.splitlines()
    for name in ("a.bin", "b.bin", "c.bin"):  # all 2000 bytes: hashed in full
        assert any(line.startswith("start ") and line.endswith(name) for line in lines)
        assert any(line.startswith("done ") and "/3]" in line and line.endswith(name) for line in lines)
    assert run(tree, "--replace") == 0
    lines = capsys.readouterr().out.splitlines()
    assert sorted(os.path.basename(line.split()[-1]) for line in lines if line.startswith("cached ")) == [
        "a.bin", "b.bin", "c.bin"]
    assert not any(line.startswith("start ") for line in lines)


def test_quiet_prints_no_progress(tree, capsys):
    assert run(tree, "--quiet") == 0
    assert capsys.readouterr().out == ""
