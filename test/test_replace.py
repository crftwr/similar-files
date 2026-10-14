"""Replacing the status file and the tree while another process reads them."""

import os
import sys
import threading

import pytest

from similar_files import _replace
from similar_files.status import StatusFile, read_status
from similar_files.tree import read, write


def test_a_refused_replace_is_tried_again(tmp_path, monkeypatch):
    src, dst = tmp_path / "a.tmp", tmp_path / "a"
    src.write_text("new")
    real, refusals = os.replace, [2]

    def flaky(a, b):
        if refusals[0]:
            refusals[0] -= 1
            raise PermissionError(13, "in use")
        real(a, b)

    monkeypatch.setattr(_replace.os, "replace", flaky)
    _replace.replace(src, dst, every=0)
    assert dst.read_text() == "new"


def test_it_gives_up_with_the_last_error(tmp_path, monkeypatch):
    def refuse(a, b):
        raise PermissionError(13, "in use")

    monkeypatch.setattr(_replace.os, "replace", refuse)
    with pytest.raises(PermissionError):
        _replace.replace(tmp_path / "a.tmp", tmp_path / "a", retry_for=0.05, every=0.01)


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows refuses to replace an open file")
def test_the_final_status_lands_while_a_reader_holds_the_file(tmp_path):
    """The watcher reads the status file as the scan goes; the final write
    used to be dropped when it came at that moment (xefm#620)."""
    path = tmp_path / "status.json"
    status = StatusFile(path, out=tmp_path / "r.xft", heartbeat=60)
    status.finish("running", 0)                     # a status file to hold open
    reader = open(path, "rb")
    threading.Timer(0.2, reader.close).start()
    status.finish("done", 0, groups=2, members=4)
    assert read_status(path)["state"] == "done"


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows refuses to replace an open file")
def test_the_tree_is_written_while_a_reader_holds_it(tmp_path):
    path = tmp_path / "r.xft"
    write(path, {"format": "xft", "version": 1, "nodes": []})
    reader = open(path, "rb")
    threading.Timer(0.2, reader.close).start()
    write(path, {"format": "xft", "version": 1, "nodes": [], "final": True})
    assert read(path)["final"] is True
