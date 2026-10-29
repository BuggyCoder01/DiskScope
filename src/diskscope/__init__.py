"""DiskScope: read-only disk space analysis with isolated hardware modules."""

from .cancellation import CancelToken
from .models import (
    DirNode,
    FileEntry,
    FileKind,
    Progress,
    SkippedItem,
    SpaceScanResult,
)

__all__ = [
    "CancelToken",
    "DirNode",
    "FileEntry",
    "FileKind",
    "Progress",
    "SkippedItem",
    "SpaceScanResult",
]

__version__ = "0.1.0"
