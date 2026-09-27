"""The command line: parse arguments, call the library, print."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path
from typing import Optional, Sequence

from . import __version__
from .cache import Cache, CacheError
from .identical import find_identical
from .model import Progress
from .playlist import SUFFIX, is_our_playlist, write_playlists
from .registry import ExtractorUnavailable, extractors, get_extractor
from .similar import find_similar
from .source import LocalFile, WalkStats, walk

logger = logging.getLogger("similar_files")


def _parse_param(text: str) -> tuple[str, str]:
    key, sep, value = text.partition("=")
    if not sep or not key:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {text!r}")
    return key.strip(), value.strip()


def _threshold(text: str) -> float:
    value = float(text)
    if not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError("threshold is a similarity between 0 and 1")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="similar-files",
        description="Find identical or similar files and write each group as an .m3u8 playlist.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="scan folders and write one playlist per group")
    scan.add_argument("roots", nargs="+", metavar="ROOT", help="folders (or files) to scan")
    scan.add_argument("-o", "--out", required=True, metavar="DIR", help="folder to write the playlists into")
    scan.add_argument(
        "-m", "--method", default="identical", metavar="NAME",
        help="'identical' (default), or an extractor such as 'image' (see: similar-files extractors)",
    )
    scan.add_argument(
        "-t", "--threshold", type=_threshold, metavar="SIM",
        help="similarity 0–1 a member must reach (default: the extractor's own)",
    )
    scan.add_argument(
        "-p", "--param", type=_parse_param, action="append", default=[], metavar="KEY=VALUE",
        help="extractor parameter, e.g. hash_size=16 (repeatable)",
    )
    scan.add_argument(
        "-r", "--reference", action="append", default=[], metavar="FILE",
        help="find copies of this file instead of discovering groups (repeatable)",
    )
    scan.add_argument("--cache", metavar="PATH", help="cache database (default: the per-user cache)")
    scan.add_argument("--no-cache", action="store_true", help="neither read nor write the cache")
    scan.add_argument("--workers", type=int, metavar="N", help="parallel readers for local files")
    scan.add_argument("--remote-workers", type=int, metavar="N", help="parallel readers for remote files")
    scan.add_argument("--follow-symlinks", action="store_true", help="follow symbolic links (each target once)")
    scan.add_argument(
        "--remote-root", action="append", default=[], metavar="DIR",
        help="treat files under DIR as remote, e.g. a network mount (repeatable)",
    )
    scan.add_argument(
        "--include-remote", action="store_true",
        help="extract features of remote files too (downloads them)",
    )
    scan.add_argument("--min-size", type=int, default=1, metavar="BYTES", help="ignore smaller files (default 1)")
    scan.add_argument(
        "--replace", action="store_true",
        help="remove playlists an earlier run wrote in --out before writing new ones",
    )
    _add_verbosity(scan)

    ex = sub.add_parser("extractors", help="list the extractors and whether they are installed")
    _add_verbosity(ex)

    cache = sub.add_parser("cache", help="show or trim the cache")
    cache.add_argument("action", choices=["info", "gc"], nargs="?", default="info")
    cache.add_argument("--cache", metavar="PATH", help="cache database (default: the per-user cache)")
    cache.add_argument("--max-age-days", type=float, metavar="DAYS", help="gc: drop rows not seen for DAYS")
    cache.add_argument("--max-size-mb", type=float, metavar="MB", help="gc: drop the oldest features beyond MB")
    _add_verbosity(cache)
    return parser


def _add_verbosity(p: argparse.ArgumentParser) -> None:
    p.add_argument("-q", "--quiet", action="store_true", help="only errors")
    p.add_argument("-v", "--verbose", action="store_true", help="more detail")


class _ProgressLine:
    """A single self-overwriting status line on stderr, when stderr is a terminal."""

    def __init__(self, enabled: bool):
        self.enabled = enabled and sys.stderr.isatty()
        self._width = 0

    def __call__(self, p: Progress) -> None:
        if not self.enabled:
            return
        text = f"{p.phase}: {p.done}/{p.total}" if p.total else f"{p.phase}: {p.done}"
        sys.stderr.write("\r" + text.ljust(self._width))
        sys.stderr.flush()
        self._width = len(text)

    def clear(self) -> None:
        if self.enabled and self._width:
            sys.stderr.write("\r" + " " * self._width + "\r")
            sys.stderr.flush()
            self._width = 0


def _cmd_scan(args: argparse.Namespace) -> int:
    out = Path(args.out).expanduser()
    if out.exists() and not out.is_dir():
        logger.error("--out %s is not a folder", out)
        return 2
    ours = [p for p in out.glob("*" + SUFFIX) if is_our_playlist(p)] if out.is_dir() else []
    if ours and not args.replace:
        logger.error("%s already holds %d playlist(s) from an earlier run; pass --replace to remove them", out, len(ours))
        return 2

    extractor = None
    if args.method != "identical":
        try:
            extractor = get_extractor(args.method, **dict(args.param))
        except (KeyError, ValueError, ExtractorUnavailable) as exc:
            logger.error("%s", exc.args[0] if isinstance(exc, KeyError) else exc)
            return 2
    elif args.param or args.threshold is not None:
        logger.error("--param and --threshold apply to extractors, not to 'identical'")
        return 2

    references = []
    for ref in args.reference:
        try:
            references.append(LocalFile.from_path(ref))
        except OSError as exc:
            logger.error("reference %s: %s", ref, exc)
            return 2

    # The first Ctrl-C cancels cleanly (groups found so far are still written);
    # a second one interrupts.
    stop = threading.Event()

    def on_sigint(signum, frame):
        stop.set()
        signal.signal(signal.SIGINT, signal.default_int_handler)

    previous = signal.signal(signal.SIGINT, on_sigint)
    progress = _ProgressLine(not args.quiet)
    stats = WalkStats()
    items = walk(
        args.roots,
        follow_symlinks=args.follow_symlinks,
        remote_roots=args.remote_root,
        stats=stats,
        cancel=stop.is_set,
    )
    try:
        cache = False if args.no_cache else Cache(args.cache)
    except CacheError as exc:
        logger.error("%s", exc)
        return 2
    try:
        common = dict(
            references=references, cache=cache, workers=args.workers, remote_workers=args.remote_workers,
            progress=progress, cancel=stop.is_set,
        )
        if extractor is None:
            result = find_identical(items, min_size=args.min_size, **common)
        else:
            result = find_similar(
                (i for i in items if i.size >= args.min_size), extractor,
                threshold=args.threshold, include_remote=args.include_remote, **common,
            )
    finally:
        progress.clear()
        signal.signal(signal.SIGINT, previous)
        if cache is not False:
            cache.close()

    if args.replace:
        for p in ours:
            p.unlink()
    written = write_playlists(result.groups, out)

    if stats.hardlink_aliases:
        logger.info("%d hard link(s) counted once", stats.hardlink_aliases)
    if stats.symlinks_skipped and not args.follow_symlinks:
        logger.info("%d symbolic link(s) not followed", stats.symlinks_skipped)
    if result.skipped_remote:
        logger.warning("%d remote file(s) skipped; pass --include-remote to download and compare them", result.skipped_remote)
    if result.unreadable:
        logger.warning("%d file(s) could not be decoded", result.unreadable)
    errors = result.errors + stats.errors
    if errors:
        logger.warning("%d file(s) or folder(s) could not be read", errors)
    if result.cancelled:
        logger.warning("cancelled; the groups found so far are written")
    if not args.quiet:
        members = sum(len(g.members) for g in result.groups)
        print(f"{len(written)} group(s), {members} member(s) → {out}")
    return 130 if result.cancelled else (1 if errors else 0)


def _cmd_extractors(args: argparse.Namespace) -> int:
    print(f"{'identical':<12} available  Identical files: the same bytes")
    for name, cls in sorted(extractors().items()):
        state = "available" if cls.is_available() else "missing  "
        line = f"{name:<12} {state}  {cls.description}"
        if not cls.is_available():
            line += f"  ({cls.install_hint()})"
        print(line)
        if args.verbose:
            params = ", ".join(f"{k}={v}" for k, v in cls.default_params.items())
            print(f"{'':<12} {'':<9}  params: {params}; default threshold {cls.default_threshold}")
    return 0


def _cmd_cache(args: argparse.Namespace) -> int:
    try:
        cache = Cache(args.cache)
    except CacheError as exc:
        logger.error("%s", exc)
        return 2
    with cache:
        if args.action == "gc":
            if args.max_age_days is None and args.max_size_mb is None:
                logger.error("gc needs --max-age-days and/or --max-size-mb")
                return 2
            max_bytes = int(args.max_size_mb * 1024 * 1024) if args.max_size_mb is not None else None
            r = cache.gc(max_age_days=args.max_age_days, max_bytes=max_bytes)
            print(f"removed {r.files_removed} file row(s), {r.features_removed} feature row(s)")
        s = cache.stats()
        print(f"{s['path']}\n{s['files']} file(s), {s['features']} feature(s), {s['bytes'] / 1024 / 1024:.1f} MB")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    level = logging.ERROR if args.quiet else logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s", stream=sys.stderr)
    if args.command == "scan":
        return _cmd_scan(args)
    if args.command == "extractors":
        return _cmd_extractors(args)
    return _cmd_cache(args)
