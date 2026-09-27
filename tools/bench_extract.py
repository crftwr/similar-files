"""Measure how feature extraction scales with the number of worker threads.

Generates media into a scratch folder (default temp/bench), then runs
extraction with no cache at several worker counts, and prints the wall time,
files per second, and CPU time / wall time. When CPU/wall grows with the
workers, threads scale; when it stalls near 1.0 while workers grow, the GIL
is the bottleneck and a process pool would help.

    python tools/bench_extract.py image --files 400
    python tools/bench_extract.py video --files 24
    python tools/bench_extract.py identical --files 400
"""

from __future__ import annotations

import argparse
import os
import random
import resource
import shutil
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import similar_files as sf  # noqa: E402


def cpu_seconds() -> float:
    ru = resource.getrusage(resource.RUSAGE_SELF)
    ch = resource.getrusage(resource.RUSAGE_CHILDREN)
    return ru.ru_utime + ru.ru_stime + ch.ru_utime + ch.ru_stime


def make_images(d: str, n: int) -> None:
    from PIL import Image, ImageDraw

    for i in range(n):
        path = os.path.join(d, f"img{i:05}.jpg")
        if os.path.exists(path):
            continue
        rnd = random.Random(i)
        img = Image.new("RGB", (3000, 2000), tuple(rnd.randrange(256) for _ in range(3)))
        dr = ImageDraw.Draw(img)
        for _ in range(40):
            x, y = rnd.randrange(3000), rnd.randrange(2000)
            dr.ellipse([x, y, x + rnd.randrange(100, 900), y + rnd.randrange(100, 900)],
                       fill=tuple(rnd.randrange(256) for _ in range(3)))
        img.save(path, quality=90)


def make_videos(d: str, n: int) -> None:
    sources = ["testsrc2", "mandelbrot", "life", "cellauto", "smptehdbars", "rgbtestsrc"]
    for i in range(n):
        path = os.path.join(d, f"vid{i:03}.mp4")
        if os.path.exists(path):
            continue
        src = f"{sources[i % len(sources)]}=size=1280x720:rate=30"
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i", src, "-t", str(20 + i % 7),
                        "-vf", f"hue=h={i * 37 % 360}", "-c:v", "libx264", "-preset", "veryfast",
                        "-pix_fmt", "yuv420p", path], check=True)


def make_blobs(d: str, n: int) -> None:
    for i in range(n):
        path = os.path.join(d, f"blob{i:05}.bin")
        if not os.path.exists(path):
            with open(path, "wb") as f:
                f.write(random.Random(i % (n // 2 or 1)).randbytes(4 * 1024 * 1024))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("method", choices=["image", "video", "audio", "identical"])
    p.add_argument("--files", type=int, default=200)
    p.add_argument("--dir", default=os.path.join("temp", "bench"))
    p.add_argument("--workers", default="1,2,4,8,16")
    args = p.parse_args()

    d = os.path.join(args.dir, args.method)
    os.makedirs(d, exist_ok=True)
    t = time.monotonic()
    {"image": make_images, "video": make_videos, "identical": make_blobs}[args.method](d, args.files)
    print(f"{args.files} file(s) in {d} (made in {time.monotonic() - t:.1f}s); {os.cpu_count()} CPUs")
    print(f"{'workers':>7} {'wall s':>8} {'files/s':>8} {'cpu/wall':>8}")
    for w in [int(x) for x in args.workers.split(",")]:
        items = list(sf.walk([d]))
        c0, t0 = cpu_seconds(), time.monotonic()
        if args.method == "identical":
            sf.find_identical(items, cache=False, workers=w)
        else:
            sf.extract_features(items, args.method, cache=False, workers=w)
        wall = time.monotonic() - t0
        print(f"{w:>7} {wall:>8.2f} {len(items) / wall:>8.1f} {(cpu_seconds() - c0) / wall:>8.2f}")


if __name__ == "__main__":
    main()
