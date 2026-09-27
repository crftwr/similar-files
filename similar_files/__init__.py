"""similar-files: find files that are the same, or look the same, and write each group as a playlist.

The public API is what this module exports. Everything else may change.
"""

from .cache import Cache, CacheError, default_cache_dir, resolve_cache_path
from .identical import find_identical
from .model import Group, Member, Progress, ScanResult
from .playlist import our_playlists, read_playlist, remove_playlists, write_playlists
from .registry import (
    ExtractionFailed,
    Extractor,
    ExtractorUnavailable,
    extractors,
    get_extractor,
    register,
)
from .similar import FeatureSet, extract_features, find_similar, group_features
from .source import FileItem, LocalFile, WalkStats, walk

__version__ = "0.1.0"

__all__ = [
    "Cache",
    "CacheError",
    "ExtractionFailed",
    "Extractor",
    "ExtractorUnavailable",
    "FeatureSet",
    "FileItem",
    "Group",
    "LocalFile",
    "Member",
    "Progress",
    "ScanResult",
    "WalkStats",
    "default_cache_dir",
    "extract_features",
    "extractors",
    "find_identical",
    "find_similar",
    "get_extractor",
    "group_features",
    "our_playlists",
    "read_playlist",
    "register",
    "remove_playlists",
    "resolve_cache_path",
    "walk",
    "write_playlists",
]
