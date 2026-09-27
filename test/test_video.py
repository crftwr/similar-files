"""The video extractor: re-encoded and resized copies group with their original, other videos do not."""

import os
import shutil
import subprocess

import pytest

if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
    pytest.skip("ffmpeg and ffprobe are not on PATH", allow_module_level=True)

from similar_files import find_similar, get_extractor, walk  # noqa: E402
from similar_files.registry import ExtractionFailed  # noqa: E402


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *args], check=True)


@pytest.fixture(scope="module")
def videos(tmp_path_factory):
    d = tmp_path_factory.mktemp("videos")
    ffmpeg("-f", "lavfi", "-i", "testsrc2=size=320x180:rate=15:duration=4",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "original.mp4"))
    ffmpeg("-i", str(d / "original.mp4"), "-vf", "scale=160:90", "-c:v", "libx264", "-crf", "35", str(d / "small.mp4"))
    ffmpeg("-i", str(d / "original.mp4"), "-c:v", "mpeg4", "-q:v", "8", str(d / "remuxed.mkv"))
    ffmpeg("-f", "lavfi", "-i", "mandelbrot=size=320x180:rate=15", "-t", "4",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", str(d / "different.mp4"))
    (d / "broken.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42 not a real video")
    return d


def names(g):
    return sorted(os.path.basename(i.uri) for i in g.items)


def test_copies_group_and_a_different_video_does_not(videos, cache_path):
    result = find_similar(walk([videos]), "video", cache=cache_path)
    assert [names(g) for g in result.groups] == [["original.mp4", "remuxed.mkv", "small.mp4"]]
    assert all(m.similarity >= 0.85 for m in result.groups[0].members)
    assert result.unreadable == 1


def test_similarity_scale(videos):
    ex = get_extractor("video")
    a = ex.extract_file(str(videos / "original.mp4"))
    assert ex.similarity(a, a) == 1.0
    assert ex.similarity(a, ex.extract_file(str(videos / "small.mp4"))) > 0.9
    assert ex.similarity(a, ex.extract_file(str(videos / "different.mp4"))) < 0.5


def test_a_file_that_is_not_a_video_fails_cleanly(videos):
    with pytest.raises(ExtractionFailed):
        get_extractor("video").extract_file(str(videos / "broken.mp4"))


def test_remote_items_are_extracted_from_a_temporary_copy(videos, cache_path):
    """An item with no local_path is downloaded once, to a temporary file."""
    from similar_files import LocalFile

    class Remote:
        is_remote = True
        validator = None

        def __init__(self, path):
            local = LocalFile.from_path(path)
            self.uri = "ssh://host" + local.path
            self.size, self.mtime_ns, self._path = local.size, local.mtime_ns, local.path
            self.opened = 0

        def open(self):
            self.opened += 1
            return open(self._path, "rb")

        def content_hash(self):
            return None

    items = [Remote(videos / n) for n in ("original.mp4", "small.mp4", "different.mp4")]
    result = find_similar(items, "video", include_remote=True, cache=cache_path)
    assert [sorted(i.uri.rsplit("/", 1)[1] for i in g.items) for g in result.groups] == [["original.mp4", "small.mp4"]]
    assert [i.opened for i in items] == [1, 1, 1]
    # Cached: a second scan reads nothing.
    for i in items:
        i.opened = 0
    find_similar(items, "video", include_remote=True, cache=cache_path)
    assert [i.opened for i in items] == [0, 0, 0]


def test_parameters():
    with pytest.raises(ValueError):
        get_extractor("video", frames=1)
    assert get_extractor("video", frames=4).params_digest != get_extractor("video").params_digest
