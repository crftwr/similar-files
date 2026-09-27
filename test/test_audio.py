"""The audio extractor: the same recording re-encoded groups with its original, other audio does not."""

import os
import shutil
import subprocess

import pytest

if not (shutil.which("fpcalc") and shutil.which("ffmpeg")):
    pytest.skip("fpcalc (Chromaprint) and ffmpeg are not on PATH", allow_module_level=True)

from similar_files import find_similar, get_extractor, walk  # noqa: E402
from similar_files.registry import ExtractionFailed  # noqa: E402


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *args], check=True)


def tune(path, a, b, seconds=20):
    """A tone that steps through pitches: enough structure for Chromaprint."""
    expr = f"0.4*sin(2*PI*({a}+90*mod(floor(t*4),7))*t)+0.2*sin(2*PI*({b}-60*mod(floor(t*3),5))*t)"
    ffmpeg("-f", "lavfi", "-i", f"aevalsrc='{expr}':d={seconds}", str(path))


@pytest.fixture(scope="module")
def songs(tmp_path_factory):
    d = tmp_path_factory.mktemp("songs")
    tune(d / "original.wav", 200, 900)
    ffmpeg("-i", str(d / "original.wav"), "-b:a", "64k", str(d / "low.mp3"))
    ffmpeg("-i", str(d / "original.wav"), "-f", "lavfi", "-t", "20", "-i", "anoisesrc=a=0.02:seed=3",
           "-filter_complex", "[0][1]amix", str(d / "noisy.m4a"))
    tune(d / "different.wav", 330, 700)
    tune(d / "short.wav", 200, 900, seconds=8)   # the same tune, but a different length
    (d / "broken.mp3").write_bytes(b"ID3 not really an mp3")
    return d


def names(g):
    return sorted(os.path.basename(i.uri) for i in g.items)


def test_reencodes_group_and_different_audio_does_not(songs, cache_path):
    result = find_similar(walk([songs]), "audio", cache=cache_path)
    assert [names(g) for g in result.groups] == [["low.mp3", "noisy.m4a", "original.wav"]]
    assert all(m.similarity >= 0.7 for m in result.groups[0].members)
    assert result.unreadable == 1


def test_similarity_scale(songs):
    ex = get_extractor("audio")
    a = ex.extract_file(str(songs / "original.wav"))
    assert ex.similarity(a, a) == 1.0
    assert ex.similarity(a, ex.extract_file(str(songs / "low.mp3"))) > 0.9
    assert ex.similarity(a, ex.extract_file(str(songs / "different.wav"))) < 0.5
    # Durations 20 s and 8 s: different recordings, whatever the start sounds like.
    assert ex.similarity(a, ex.extract_file(str(songs / "short.wav"))) == 0.0


def test_a_shifted_copy_still_matches(songs, tmp_path):
    ffmpeg("-i", str(songs / "original.wav"), "-af", "adelay=500", "-t", "20", str(tmp_path / "late.wav"))
    ex = get_extractor("audio")
    assert ex.similarity(ex.extract_file(str(songs / "original.wav")), ex.extract_file(str(tmp_path / "late.wav"))) > 0.7


def test_a_file_that_is_not_audio_fails_cleanly(songs):
    with pytest.raises(ExtractionFailed):
        get_extractor("audio").extract_file(str(songs / "broken.mp3"))
