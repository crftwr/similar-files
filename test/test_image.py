"""The image extractor: near copies group with their original, different pictures do not."""

import os

import pytest

PIL = pytest.importorskip("PIL")
pytest.importorskip("imagehash")

from PIL import Image, ImageDraw, ImageFilter  # noqa: E402

from similar_files import find_similar, get_extractor, walk  # noqa: E402


def scene(seed: int, size=(640, 480)) -> Image.Image:
    import random

    rnd = random.Random(seed)
    img = Image.new("RGB", size, (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    d = ImageDraw.Draw(img)
    for _ in range(12):
        x0, y0 = rnd.randrange(size[0]), rnd.randrange(size[1])
        x1, y1 = x0 + rnd.randrange(40, 300), y0 + rnd.randrange(40, 300)
        colour = (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256))
        if rnd.random() < 0.5:
            d.ellipse([x0, y0, x1, y1], fill=colour)
        else:
            d.rectangle([x0, y0, x1, y1], fill=colour)
    return img


def names(g):
    return [os.path.basename(i.uri) for i in g.items]


@pytest.fixture
def photos(tmp_path):
    original = scene(1)
    original.save(tmp_path / "original.png")
    original.resize((320, 240), Image.LANCZOS).save(tmp_path / "small.jpg", quality=70)
    original.save(tmp_path / "recompressed.jpg", quality=40)
    original.filter(ImageFilter.GaussianBlur(1)).save(tmp_path / "blurred.webp")
    original.crop((8, 6, 632, 474)).save(tmp_path / "cropped.png")
    scene(2).save(tmp_path / "different.png")
    scene(3).save(tmp_path / "another.jpg")
    (tmp_path / "broken.jpg").write_bytes(b"\xff\xd8 this is not really a jpeg")
    return tmp_path


def test_near_copies_group_with_their_original(photos, cache_path):
    result = find_similar(walk([photos]), "image", cache=cache_path)
    assert len(result.groups) == 1
    g = result.groups[0]
    assert g.anchor.size == max(i.size for i in g.items)  # the largest file anchors
    assert set(names(g)) == {"original.png", "small.jpg", "recompressed.jpg", "blurred.webp", "cropped.png"}
    assert all(0.8 <= m.similarity <= 1.0 for m in g.members)
    assert result.unreadable == 1


def test_different_pictures_do_not_group(photos, cache_path):
    ex = get_extractor("image")
    with open(photos / "original.png", "rb") as f:
        a = ex.extract(f)
    for other in ("different.png", "another.jpg"):
        with open(photos / other, "rb") as f:
            assert ex.similarity(a, ex.extract(f)) < 0.5


@pytest.mark.parametrize("algorithm", ["phash", "dhash", "ahash", "whash"])
def test_every_algorithm_groups_a_resized_copy(tmp_path, algorithm):
    scene(4).save(tmp_path / "a.png")
    scene(4).resize((200, 150)).save(tmp_path / "b.png")
    scene(5).save(tmp_path / "c.png")
    result = find_similar(walk([tmp_path]), "image", params={"algorithm": algorithm}, cache=False)
    assert [sorted(names(g)) for g in result.groups] == [["a.png", "b.png"]]


def test_exif_rotation_is_applied(tmp_path):
    img = scene(6)
    img.save(tmp_path / "upright.jpg", quality=90)
    exif = Image.Exif()
    exif[0x0112] = 6  # stored rotated; displays upright after a 90° turn
    img.transpose(Image.ROTATE_90).save(tmp_path / "rotated.jpg", quality=90, exif=exif)
    result = find_similar(walk([tmp_path]), "image", cache=False)
    assert len(result.groups) == 1


def test_bad_params():
    with pytest.raises(ValueError):
        get_extractor("image", algorithm="nope")
    with pytest.raises(ValueError):
        get_extractor("image", hash_size=2)
