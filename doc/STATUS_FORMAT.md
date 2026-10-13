# Status file format

`similar-files scan --status FILE` keeps a scan's progress in `FILE` as JSON,
for a program that watches from outside and may not stay attached: XeFM
starts a scan as its own process, so closing XeFM does not stop it, and reads
this file to show how the scan is going. `--stop-file FILE` is the other
direction: create that file to stop the scan, the way Ctrl-C does.

Neither needs the watcher to share a process, a pipe or a signal with the
scan, so both work the same on every platform and across a restart of the
watcher. The library side is `similar_files.status`: `StatusFile` writes the
file, `read_status()` reads it, and `StopFile` is the cancel check.

## Writing

The file is written to `FILE.tmp` and moved over `FILE` with `os.replace`, so
a reader never sees half of it. It is written:

- when the scan starts (`state` `"running"`);
- when progress moves into a new phase, and otherwise at most once a second;
- every 5 seconds even when nothing moves, because an extractor can spend
  minutes on one video. A reader can tell a scan that is busy from one that
  died: `updated` stops changing, and `pid` no longer runs;
- once more when it ends, with the final `state`.

A failure to write the file does not stop the scan.

## Fields (version 1)

| Field | |
|---|---|
| `version` | `1`. A reader should ignore a file with any other version |
| `state` | `"running"`, then `"done"`, `"cancelled"` or `"failed"` |
| `pid` | The scanning process |
| `started`, `updated` | Seconds since the epoch |
| `phase` | The latest progress report's phase: `"cache"`, `"sample"`, `"hash"`, `"extract"` or `"group"`, or `null` before the first |
| `done`, `total` | That phase's counts. `total` is `0` when not known |
| `out` | The playlist folder |
| `groups`, `members` | Written at the end: the playlists written, and the members in them (anchors not counted) |
| `exit_code` | Written at the end: what the command returns. `0` done, `1` done but some files or folders could not be read, `2` bad options, `130` cancelled |

`"done"` covers exit codes 0 and 1. A scan whose options are wrong ends as
`"failed"` with `exit_code` 2 before it reads anything.

## Stopping

`--stop-file FILE` is checked about twice a second. Once the file exists, the
scan stops as it would on Ctrl-C: work in progress is abandoned, the groups
found so far are written, and the status ends as `"cancelled"` with
`exit_code` 130. Finished work stays in the cache, so running the same scan
again resumes quickly. The scan does not delete the stop file.
