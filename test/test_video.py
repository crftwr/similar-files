"""The video extractor: re-encoded and resized copies, and clips cut out of a video, group with it; other videos do not."""

import os
import shutil
import subprocess

import pytest

if not shutil.which("ffmpeg"):
    pytest.skip("ffmpeg is not on PATH", allow_module_level=True)

from similar_files import find_similar, get_extractor, walk  # noqa: E402
from similar_files.registry import ExtractionFailed  # noqa: E402


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *args], check=True)


#: Distinct, never-flat shots of 4 s each. An encoder puts a keyframe at each cut.
_SHOTS = ["testsrc2", "mandelbrot", "smptehdbars", "rgbtestsrc", "testsrc", "yuvtestsrc", "pal100bars", "sierpinski"]


def make(out, shots, *, gop=30, seconds=4):
    """Concatenate lavfi shots (320×180, 15 fps) into one H.264 video."""
    args = []
    for shot in shots:
        args += ["-f", "lavfi", "-t", str(seconds), "-i", f"{shot}=size=320x180:rate=15"]
    chain = "".join(f"[{i}:v]scale=320:180,setsar=1,scroll=horizontal=0.002,format=yuv420p[v{i}];" for i in range(len(shots)))
    chain += "".join(f"[v{i}]" for i in range(len(shots))) + f"concat=n={len(shots)}:v=1[o]"
    ffmpeg(*args, "-filter_complex", chain, "-map", "[o]", "-c:v", "libx264", "-g", str(gop), str(out))


@pytest.fixture(scope="module")
def videos(tmp_path_factory):
    d = tmp_path_factory.mktemp("videos")
    make(d / "original.mp4", _SHOTS[:5])  # 20 s, a keyframe every 2 s and at each cut
    ffmpeg("-i", str(d / "original.mp4"), "-vf", "scale=160:90", "-c:v", "libx264", "-g", "45", "-crf", "35",
           str(d / "small.mp4"))
    ffmpeg("-i", str(d / "original.mp4"), "-c:v", "mpeg4", "-q:v", "8", str(d / "remuxed.mkv"))
    # 6 s to 16 s of the original, re-encoded with its keyframes elsewhere.
    ffmpeg("-ss", "6", "-i", str(d / "original.mp4"), "-t", "10", "-c:v", "libx264", "-g", "40", str(d / "clip.mp4"))
    make(d / "different.mp4", _SHOTS[5:], gop=40)
    # The clip followed by a video of its own length: half of it is in the original.
    (d / "mixed").mkdir()
    ffmpeg("-i", str(d / "clip.mp4"), "-i", str(d / "different.mp4"), "-filter_complex", "[0:v][1:v]concat=n=2:v=1[o]",
           "-map", "[o]", "-c:v", "libx264", str(d / "mixed" / "half.mp4"))
    # Other keyframe spacings: every 1 s, and every 10 s (with the original's shots and 3 more).
    (d / "gop").mkdir()
    ffmpeg("-i", str(d / "original.mp4"), "-c:v", "libx264", "-g", "15", str(d / "gop" / "dense.mp4"))
    make(d / "gop" / "sparse_longer.mp4", _SHOTS, gop=150)
    (d / "mixed" / "broken.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42 not a real video")
    return d


def names(g):
    return sorted(os.path.basename(i.uri) for i in g.items)


def top_level(d):
    return [p for p in walk([d]) if os.path.dirname(p.uri) == str(d)]


def test_copies_and_a_clip_group_and_a_different_video_does_not(videos, cache_path):
    result = find_similar(top_level(videos), "video", cache=cache_path)
    assert [names(g) for g in result.groups] == [["clip.mp4", "original.mp4", "remuxed.mkv", "small.mp4"]]
    assert all(m.similarity >= 0.9 for m in result.groups[0].members)


def features(videos, *names):
    ex = get_extractor("video")
    return ex, [ex.extract_file(str(videos / n)) for n in names]


def test_similarity_scale(videos):
    ex, (a, small, clip, different) = features(videos, "original.mp4", "small.mp4", "clip.mp4", "different.mp4")
    assert ex.similarity(a, a) == 1.0
    assert ex.similarity(a, small) > 0.9
    assert ex.similarity(a, different) < 0.5


def test_a_clip_is_contained_in_either_direction(videos):
    """The clip covers half of the original, at another offset, with its keyframes elsewhere."""
    ex, (a, clip) = features(videos, "original.mp4", "clip.mp4")
    assert ex.similarity(a, clip) > 0.9
    assert ex.similarity(clip, a) == ex.similarity(a, clip)


def test_a_video_half_from_elsewhere_scores_below_the_default(videos):
    ex, (a, clip, half) = features(videos, "original.mp4", "clip.mp4", "mixed/half.mp4")
    assert ex.similarity(clip, half) > 0.9  # all of the clip is in it
    assert ex.similarity(a, half) < ex.default_threshold  # only half of it is in the original


def test_a_video_matches_the_longer_one_containing_it_whatever_its_keyframes(videos):
    """The longer video has fewer keyframes than the one it contains: the shorter one is still the measure."""
    ex, (a, sparse) = features(videos, "original.mp4", "gop/sparse_longer.mp4")
    assert len(ex._decode(sparse).frames) < len(ex._decode(a).frames)
    assert ex.similarity(a, sparse) > 0.9
    assert ex.similarity(sparse, a) == ex.similarity(a, sparse)


def test_copies_with_other_keyframe_spacing_match(videos):
    ex, (a, dense, small, clip) = features(videos, "original.mp4", "gop/dense.mp4", "small.mp4", "clip.mp4")
    assert ex.similarity(dense, a) > 0.9
    assert ex.similarity(dense, small) > 0.9  # 1 s against 3 s
    assert ex.similarity(dense, clip) > 0.9


def test_a_file_that_is_not_a_video_fails_cleanly(videos, cache_path):
    with pytest.raises(ExtractionFailed):
        get_extractor("video").extract_file(str(videos / "mixed" / "broken.mp4"))
    assert find_similar(walk([videos / "mixed"]), "video", cache=cache_path).unreadable == 1


def test_a_flat_video_matches_nothing(videos, tmp_path):
    ffmpeg("-f", "lavfi", "-i", "color=black:size=320x180:rate=15:duration=4", "-c:v", "libx264",
           str(tmp_path / "black.mp4"))
    ex = get_extractor("video")
    black = ex.extract_file(str(tmp_path / "black.mp4"))
    assert ex.similarity(black, black) == 0.0
    assert ex.similarity(black, ex.extract_file(str(videos / "original.mp4"))) == 0.0


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
        get_extractor("video", interval=0)
    assert get_extractor("video", interval=2).params_digest != get_extractor("video").params_digest


def test_a_frame_many_videos_share_does_not_make_them_similar():
    """An intro all videos open with is ignored once enough videos have it; copies still match."""
    import random

    from similar_files.video import _FRAME, _HEADER

    rng = random.Random(7)
    intro = [rng.getrandbits(64) for _ in range(5)]

    def feature(hashes):
        return _HEADER.pack(0.0, 2.0 * len(hashes), len(hashes)) + b"".join(
            _FRAME.pack(2.0 * i, h) for i, h in enumerate(hashes))

    bodies = [[rng.getrandbits(64) for _ in range(5)] for _ in range(80)]
    fs = [feature(intro + body) for body in bodies]
    fs.append(feature(intro + bodies[0]))  # a copy of the first
    ex = get_extractor("video")
    # Two videos alone: the intro is half of each, so they share half.
    assert ex.similarity(fs[0], fs[1]) == 0.5
    # Among 81 videos, the intro is common and left out.
    sims = ex.similarities(ex.prepare(fs), fs[0])
    assert sims[-1] == 1.0
    assert max(sims[1:-1]) == 0.0
