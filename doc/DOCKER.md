# Running on a NAS with Docker

> **Prototype.** The files in `docker/` may change.

Running the scan on the NAS itself reads the media from its own disks, not
over the network, and the NAS can work through a large library overnight.
The playlists land in a shared folder, where XeFM (or anything else that reads
M3U) opens them from your computer.

## Set up

On the NAS, in a checkout of this repository:

```bash
cp docker/.env.example docker/.env     # then edit it
mkdir -p <OUT_HOST_DIR> <CACHE_HOST_DIR>
docker compose -f docker/compose.yaml up --build
```

Create both folders before the first run, as the NAS user named by
`PUID`/`PGID`. If they are missing, Docker creates them owned by root, and the
scan cannot write to them.

Each method writes into its own folder: `<OUT_HOST_DIR>/identical`,
`.../image`, `.../video`, `.../audio`. Each run replaces the playlists the
previous run wrote there.

## Paths in the playlists

A playlist line is the path of the file *inside the container*. The media
folder is therefore mounted at the path **your computer** sees it under
(`MEDIA_CLIENT_DIR`, for example `/Volumes/photo` on a Mac that mounts the
`photo` share). The playlists then name paths that open as they are on that
computer.

Only one client-side spelling works per run. A Mac (`/Volumes/photo`) and a
Windows PC (`\\nas\photo`) cannot both use the same playlists.

To scan more than one shared folder, add a volume line per folder in
`docker/compose.yaml`, and list each client path in `command:`.

## Settings

All of them are in `docker/.env`; see `docker/.env.example`.

| Variable | Meaning |
|---|---|
| `METHODS` | `identical image video audio`, or any subset, in that order |
| `INTERVAL` | `0` runs once and exits. Otherwise the seconds to wait between runs |
| `WORKERS` | Parallel readers. Lower it on a NAS with a small CPU |
| `NICE` | CPU priority of the scan (0–19), so the NAS stays responsive |
| `EXTRA_ARGS` | More options for every scan, such as `-v` or `--min-size 100000` |

Other commands of the CLI run through the same image:

```bash
docker compose -f docker/compose.yaml run --rm similar-files extractors
docker compose -f docker/compose.yaml run --rm similar-files cache --help
```

## Stopping

`docker compose stop` cancels a running scan the way Ctrl-C does: the groups
found so far are written, the cache keeps every feature already extracted, and
the next run resumes from there. Methods not yet started are skipped.

## The cache

The container keeps its own cache in `CACHE_HOST_DIR`. It is not shared with
the cache on your computer. Keep it on the NAS's local disk, not on a network
share, because SQLite over SMB or NFS is unreliable.
