# Parallel extraction: measurements

CLAUDE.md says to start with a thread pool and move to a process pool only
when a measurement shows the GIL is the bottleneck. These measurements say it
is not. Re-run them before changing the pool:

```bash
python tools/bench_extract.py image --files 300
python tools/bench_extract.py video --files 24 --workers 1,2,4,8,12
python tools/bench_extract.py identical --files 300 --workers 1,2,4,8
```

The tool generates its media under `temp/bench/`, extracts with no cache, and
prints wall time, files per second, and CPU time divided by wall time. With a
thread pool, CPU/wall that grows with the workers means the threads really
run at once. If it stalled near 1.0 while the workers grew, the GIL would be
serializing them.

## Results (2026-09-27, Apple silicon, 10 cores, Python 3.14)

**Images** (300 JPEGs, 3000×2000):

| workers | wall s | files/s | CPU/wall |
|---:|---:|---:|---:|
| 1 | 1.53 | 196 | 0.95 |
| 2 | 0.60 | 504 | 2.00 |
| 4 | 0.32 | 937 | 3.90 |
| 8 | 0.22 | 1353 | 7.35 |
| 16 | 0.20 | 1467 | 8.33 |

This is close to linear up to 8 workers. Pillow releases the GIL while it
decodes, and draft mode keeps JPEG decoding cheap.

**Video** (24 H.264 files, 1280×720, 20–26 s):

| workers | wall s | files/s | CPU/wall |
|---:|---:|---:|---:|
| 1 | 34.2 | 0.7 | 3.49 |
| 2 | 23.6 | 1.0 | 5.92 |
| 4 | 20.3 | 1.2 | 7.57 |
| 8 | 19.5 | 1.2 | 8.08 |

The work is inside ffmpeg, which is multithreaded on its own, so the machine
is saturated at about 4 workers. Python is idle, waiting on subprocesses. The
cost is decoding from the keyframe before each sampled frame. Taking
keyframes only (`-skip_frame nokey`) would be much faster, but a re-encoded
copy has its keyframes elsewhere, so the frames would no longer line up.

**Identical files** (300 × 4 MB, in the page cache):

| workers | wall s | files/s | CPU/wall |
|---:|---:|---:|---:|
| 1 | 0.97 | 309 | 1.00 |
| 2 | 0.49 | 612 | 1.93 |
| 4 | 0.24 | 1243 | 3.86 |
| 8 | 0.16 | 1928 | 6.85 |

`hashlib` releases the GIL on large buffers. On a real disk, I/O sets the
limit, not the CPU.

## Conclusions

- Keep the thread pool. No measured case is limited by the GIL.
- The default of `min(8, CPU count)` local workers is right: beyond that,
  images gain little, and video is already limited by ffmpeg's own threads.
- Remote workers (default 4) have not been measured against a real S3 bucket
  or SSH host. Measure before changing that default.
