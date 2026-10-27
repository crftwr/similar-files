"""The audio extractor: copies, excerpts and longer versions group with the original, other audio does not."""

import math
import os
import random
import shutil
import struct
import subprocess
import wave

import pytest

if not (shutil.which("fpcalc") and shutil.which("ffmpeg")):
    pytest.skip("fpcalc (Chromaprint) and ffmpeg are not on PATH", allow_module_level=True)

from similar_files import find_similar, get_extractor, walk  # noqa: E402
from similar_files.registry import ExtractionFailed  # noqa: E402


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", *args], check=True)


def melody(path, seed, seconds, rate=11025):
    """Two voices of random notes: enough structure for Chromaprint, and no repeat to align on by mistake."""
    rng = random.Random(seed)
    samples = bytearray()
    n = 0
    while n < seconds * rate:
        f1, f2 = (110 * 2 ** (rng.randrange(36) / 12) for _ in range(2))
        for k in range(n, n + int(rng.choice([0.15, 0.25, 0.4, 0.6]) * rate)):
            v = 0.4 * math.sin(2 * math.pi * f1 * k / rate) + 0.25 * math.sin(2 * math.pi * f2 * k / rate)
            samples += struct.pack("<h", int(v * 26000))
            n += 1
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(samples[:int(seconds * rate) * 2]))


@pytest.fixture(scope="module")
def songs(tmp_path_factory):
    d = tmp_path_factory.mktemp("songs")
    melody(d / "original.wav", 1, 60)
    ffmpeg("-i", str(d / "original.wav"), "-b:a", "64k", str(d / "low.mp3"))
    ffmpeg("-i", str(d / "original.wav"), "-f", "lavfi", "-t", "60", "-i", "anoisesrc=a=0.02:seed=3",
           "-filter_complex", "[0][1]amix", str(d / "noisy.m4a"))
    ffmpeg("-ss", "25.3", "-t", "15", "-i", str(d / "original.wav"), "-b:a", "96k", str(d / "excerpt.m4a"))
    melody(d / "different.wav", 2, 60)
    (d / "broken.mp3").write_bytes(b"ID3 not really an mp3")
    return d


@pytest.fixture(scope="module")
def fp(songs):
    ex = get_extractor("audio")
    return lambda path: ex.extract_file(str(songs / path))


def names(g):
    return sorted(os.path.basename(i.uri) for i in g.items)


def test_copies_and_excerpts_group_and_different_audio_does_not(songs, cache_path):
    result = find_similar(walk([songs]), "audio", cache=cache_path)
    assert [names(g) for g in result.groups] == [["excerpt.m4a", "low.mp3", "noisy.m4a", "original.wav"]]
    assert os.path.basename(result.groups[0].anchor.uri) == "original.wav"
    assert all(m.similarity >= 0.7 for m in result.groups[0].members)
    assert result.unreadable == 1


def test_similarity_scale(fp):
    ex = get_extractor("audio")
    a = fp("original.wav")
    assert ex.similarity(a, a) == 1.0
    assert ex.similarity(a, fp("low.mp3")) > 0.9
    assert ex.similarity(a, fp("different.wav")) < 0.2


def test_an_excerpt_matches_the_recording_it_was_cut_from_both_ways(fp):
    ex = get_extractor("audio")
    a, e = fp("original.wav"), fp("excerpt.m4a")
    assert ex.similarity(a, e) > 0.8
    assert ex.similarity(a, e) == ex.similarity(e, a)


def test_a_longer_version_contains_the_original(songs, fp, tmp_path):
    melody(tmp_path / "intro.wav", 3, 20)
    ffmpeg("-i", str(tmp_path / "intro.wav"), "-i", str(songs / "original.wav"),
           "-filter_complex", "[0][1]concat=n=2:v=0:a=1", str(tmp_path / "extended.mp3"))
    ex = get_extractor("audio")
    assert ex.similarity(fp("original.wav"), ex.extract_file(str(tmp_path / "extended.mp3"))) > 0.8


def test_a_partial_overlap_scores_its_share(songs, tmp_path):
    # 0–40 s and 20–60 s: half of each is in the other.
    ffmpeg("-t", "40", "-i", str(songs / "original.wav"), str(tmp_path / "first.wav"))
    ffmpeg("-ss", "20", "-i", str(songs / "original.wav"), "-b:a", "96k", str(tmp_path / "second.mp3"))
    ex = get_extractor("audio")
    sim = ex.similarity(ex.extract_file(str(tmp_path / "first.wav")), ex.extract_file(str(tmp_path / "second.mp3")))
    assert 0.35 < sim < 0.65


def test_a_shifted_copy_still_matches(songs, fp, tmp_path):
    ffmpeg("-i", str(songs / "original.wav"), "-af", "adelay=500", "-t", "60", str(tmp_path / "late.wav"))
    ex = get_extractor("audio")
    assert ex.similarity(fp("original.wav"), ex.extract_file(str(tmp_path / "late.wav"))) > 0.9


def test_length_limits_the_fingerprint(songs):
    ex = get_extractor("audio", length=20)
    whole = get_extractor("audio").extract_file(str(songs / "original.wav"))
    assert len(ex.extract_file(str(songs / "original.wav"))) < len(whole)
    with pytest.raises(ValueError):
        get_extractor("audio", length=5)


def test_a_file_that_is_not_audio_fails_cleanly(songs):
    with pytest.raises(ExtractionFailed):
        get_extractor("audio").extract_file(str(songs / "broken.mp3"))
