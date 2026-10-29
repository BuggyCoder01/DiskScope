"""Read-only filesystem space analysis.

Traversal is iterative and bounded in memory: only directory nodes and the
top-N file heaps are retained. Logical size is the default. Allocated size is
opt-in and uses Windows ``GetCompressedFileSizeW`` through a small injectable
interface so tests never touch a real device.

Hardlinks are counted once per directory entry (per name), because the scan
reports what each path logically occupies. Two hardlinked names therefore
contribute twice to the totals; the same is true of the largest-file list.
"""

from __future__ import annotations

import ctypes
import heapq
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from send2trash import send2trash

from .cancellation import CancelToken
from .models import (
    DirNode,
    FileEntry,
    FileKind,
    Progress,
    SkippedItem,
    SpaceScanResult,
    classify_file,
)

_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_PROGRESS_EVERY = 500
_DRIVE_ERRORS = {
    21,  # device not ready
    55,  # network resource unavailable
    1167,  # device not connected
    1231,  # network location unreachable
}


class ScanError(Exception):
    """Base class for space-scan failures."""


class InvalidScanRoot(ScanError, ValueError):
    """The requested scan root is missing or not a directory."""


class UnsafePathError(ValueError):
    """A deletion target failed a safety check."""


class ConfirmationRequired(RuntimeError):
    """A deletion was attempted without explicit user confirmation."""


class TrashError(RuntimeError):
    """The Recycle Bin operation failed. The file was not permanently deleted."""


@dataclass(slots=True)
class _HeapEntry:
    """Worst-first heap item: smallest size, then lexicographically largest path."""

    entry: FileEntry

    def __lt__(self, other: _HeapEntry) -> bool:
        if self.entry.size != other.entry.size:
            return self.entry.size < other.entry.size
        own_key = self.entry.path.casefold()
        other_key = other.entry.path.casefold()
        if own_key != other_key:
            return own_key > other_key
        return self.entry.path > other.entry.path


def _describe_error(exc: OSError) -> str:
    if isinstance(exc, PermissionError):
        return "permission denied"
    if isinstance(exc, FileNotFoundError):
        return "vanished during scan"
    winerror = getattr(exc, "winerror", None)
    if winerror in _DRIVE_ERRORS:
        return f"drive unavailable: {exc.strerror or exc}"
    detail = exc.strerror or str(exc)
    return f"I/O error: {detail}"


def _resolve_scan_root(root: str | os.PathLike[str]) -> Path:
    try:
        resolved = Path(root).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise InvalidScanRoot(f"scan root does not exist: {root}") from exc
    except OSError as exc:
        raise InvalidScanRoot(f"cannot resolve scan root {root}: {exc}") from exc
    try:
        is_directory = resolved.is_dir()
    except OSError as exc:
        raise InvalidScanRoot(f"cannot inspect scan root {root}: {exc}") from exc
    if not is_directory:
        raise InvalidScanRoot(f"scan root is not a directory: {root}")
    return resolved


def _sorted_entries(heap: list[_HeapEntry]) -> tuple[FileEntry, ...]:
    return tuple(
        item.entry
        for item in sorted(
            heap,
            key=lambda item: (-item.entry.size, item.entry.path.casefold(), item.entry.path),
        )
    )


def _push_entry(heap: list[_HeapEntry], entry: FileEntry, limit: int) -> None:
    if limit <= 0:
        return
    heapq.heappush(heap, _HeapEntry(entry))
    if len(heap) > limit:
        heapq.heappop(heap)


def _is_cancelled(cancel: CancelToken | None) -> bool:
    return cancel is not None and cancel.is_cancelled


def _emit(
    progress: Callable[[Progress], None] | None,
    done: int,
    detail: str,
) -> None:
    if progress is not None:
        progress(Progress("scan", done, None, detail))


def scan(
    root: str | os.PathLike[str],
    *,
    cancel: CancelToken | None = None,
    progress: Callable[[Progress], None] | None = None,
    top_n: int = 100,
    allocated: bool = False,
    allocated_size: Callable[[str], int | None] | None = None,
) -> SpaceScanResult:
    """Scan ``root`` without following links or modifying anything.

    ``items`` contains at most ``top_n`` largest files. Per-kind top-N lists are
    retained as well. A scan that is cancelled or skips any entry has
    ``complete=False``.
    """
    if top_n < 0:
        raise ValueError("top_n must be non-negative")

    allocator = allocated_size if allocated_size is not None else windows_allocated_size
    if allocated and allocator is windows_allocated_size and os.name != "nt":
        raise ValueError("allocated size is only available on Windows")

    root_path = _resolve_scan_root(root)
    root_node = DirNode(
        path=str(root_path),
        name=root_path.name or root_path.anchor or str(root_path),
    )

    skipped: list[SkippedItem] = []
    overall_heap: list[_HeapEntry] = []
    kind_heaps: dict[FileKind, list[_HeapEntry]] = {kind: [] for kind in FileKind}
    entries_seen = 0
    allocated_missing = False
    cancelled = False
    stack: list[tuple[Path, DirNode]] = [(root_path, root_node)]

    _emit(progress, 0, str(root_path))

    while stack:
        if _is_cancelled(cancel):
            cancelled = True
            break

        directory, node = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                for entry in iterator:
                    if _is_cancelled(cancel):
                        cancelled = True
                        break

                    entries_seen += 1
                    try:
                        if entry.is_symlink():
                            skipped.append(
                                SkippedItem(str(entry.path), "symbolic link not followed")
                            )
                            continue
                        is_junction = getattr(entry, "is_junction", None)
                        if is_junction is not None and is_junction():
                            skipped.append(SkippedItem(str(entry.path), "junction not followed"))
                            continue
                        entry_stat = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        skipped.append(SkippedItem(str(entry.path), _describe_error(exc)))
                        continue

                    if getattr(entry_stat, "st_file_attributes", 0) & (
                        _FILE_ATTRIBUTE_REPARSE_POINT
                    ):
                        skipped.append(SkippedItem(str(entry.path), "reparse point not followed"))
                        continue

                    if stat.S_ISDIR(entry_stat.st_mode):
                        child = DirNode(
                            path=str(entry.path),
                            name=entry.name,
                            parent=node,
                        )
                        node.children[entry.name] = child
                        ancestor: DirNode | None = node
                        while ancestor is not None:
                            ancestor.dir_count += 1
                            ancestor = ancestor.parent
                        stack.append((Path(entry.path), child))
                    elif stat.S_ISREG(entry_stat.st_mode):
                        size = int(entry_stat.st_size)
                        kind = classify_file(entry.name)
                        allocated_value: int | None = None
                        if allocated:
                            try:
                                allocated_value = allocator(str(entry.path))
                            except OSError as exc:
                                allocated_missing = True
                                skipped.append(
                                    SkippedItem(
                                        str(entry.path),
                                        f"allocated size unavailable: {_describe_error(exc)}",
                                    )
                                )
                            else:
                                if allocated_value is None:
                                    allocated_missing = True
                                    skipped.append(
                                        SkippedItem(
                                            str(entry.path),
                                            "allocated size unavailable",
                                        )
                                    )

                        ancestor = node
                        while ancestor is not None:
                            ancestor.size += size
                            if allocated_value is not None:
                                ancestor.allocated_size += allocated_value
                            ancestor.file_count += 1
                            ancestor.kind_sizes[kind] = ancestor.kind_sizes.get(kind, 0) + size
                            ancestor = ancestor.parent

                        file_entry = FileEntry(
                            path=str(entry.path),
                            size=size,
                            kind=kind,
                            allocated_size=allocated_value,
                        )
                        _push_entry(overall_heap, file_entry, top_n)
                        _push_entry(kind_heaps[kind], file_entry, top_n)
                    else:
                        skipped.append(
                            SkippedItem(
                                str(entry.path),
                                "not a regular file or directory",
                            )
                        )

                    if entries_seen % _PROGRESS_EVERY == 0:
                        _emit(progress, entries_seen, str(directory))
        except OSError as exc:
            skipped.append(SkippedItem(str(directory), _describe_error(exc)))

        if cancelled:
            break

    skipped.sort(key=lambda item: (item.path.casefold(), item.path))
    by_kind = MappingProxyType({kind: _sorted_entries(kind_heaps[kind]) for kind in FileKind})
    total_allocated: int | None = None
    if allocated and not allocated_missing:
        total_allocated = root_node.allocated_size

    result = SpaceScanResult(
        root=root_node,
        items=_sorted_entries(overall_heap),
        skipped=tuple(skipped),
        complete=not cancelled and not skipped,
        cancelled=cancelled,
        total_size=root_node.size,
        total_allocated_size=total_allocated,
        file_count=root_node.file_count,
        directory_count=root_node.dir_count + 1,
        allocated=allocated,
        top_n=top_n,
        kind_totals=MappingProxyType(dict(root_node.kind_sizes)),
        _by_kind=by_kind,
    )
    _emit(progress, entries_seen, "cancelled" if cancelled else "finished")
    return result


def node_for_path(
    result: SpaceScanResult,
    path: str | os.PathLike[str],
) -> DirNode:
    """Return the scanned directory node for ``path``.

    Relative paths are interpreted below the scan root. Paths outside the scan
    root raise :class:`UnsafePathError`.
    """
    root_path = Path(result.root.path)
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root_path / candidate
    try:
        resolved = candidate.resolve(strict=False)
    except OSError as exc:
        raise UnsafePathError(f"cannot resolve path {path}: {exc}") from exc
    try:
        relative = resolved.relative_to(root_path)
    except ValueError as exc:
        raise UnsafePathError(f"path is outside the scanned root: {candidate}") from exc

    node = result.root
    for part in relative.parts:
        child = node.child(part)
        if child is None:
            raise KeyError(f"directory is not part of the scan: {candidate}")
        node = child
    return node


def children_for(
    node: DirNode,
    kind: FileKind | str | None = None,
) -> tuple[DirNode, ...]:
    """Direct child directories, optionally only those containing ``kind``."""
    children = node.sorted_children()
    if kind is None:
        return children
    wanted = FileKind(kind)
    return tuple(child for child in children if child.kind_size(wanted) > 0)


def _protected_directories() -> tuple[Path, ...]:
    candidates: list[str] = []
    for variable in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        value = os.environ.get(variable)
        if value:
            candidates.append(value)
    if os.name == "nt":
        candidates.extend((r"C:\Windows", r"C:\Program Files", r"C:\Program Files (x86)"))

    protected: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = Path(candidate).resolve(strict=True)
        except OSError:
            continue
        if resolved not in seen:
            seen.add(resolved)
            protected.append(resolved)
    return tuple(protected)


def _stat_is_reparse(entry_stat: os.stat_result) -> bool:
    if stat.S_ISLNK(entry_stat.st_mode):
        return True
    return bool(getattr(entry_stat, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT)


def validate_deletable_path(
    path: str | os.PathLike[str],
    scan_root: str | os.PathLike[str],
) -> Path:
    """Validate a path before it is offered to the Recycle Bin.

    Rejects drive roots, the scan root, anything outside the scan root, Windows
    and Program Files directories, and reparse points.
    """
    root = _resolve_scan_root(scan_root)
    raw = Path(path).expanduser()
    try:
        raw_stat = raw.lstat()
    except FileNotFoundError as exc:
        raise UnsafePathError(f"path does not exist: {raw}") from exc
    except OSError as exc:
        raise UnsafePathError(f"cannot inspect path {raw}: {exc}") from exc
    if _stat_is_reparse(raw_stat):
        raise UnsafePathError(f"refusing to delete a reparse point: {raw}")

    try:
        candidate = raw.resolve(strict=True)
    except FileNotFoundError as exc:
        raise UnsafePathError(f"path does not exist: {raw}") from exc
    except OSError as exc:
        raise UnsafePathError(f"cannot resolve path {raw}: {exc}") from exc

    if candidate == root:
        raise UnsafePathError("refusing to delete the scan root")
    if candidate.parent == candidate:
        raise UnsafePathError("refusing to delete a drive root")
    if not candidate.is_relative_to(root):
        raise UnsafePathError(f"path is outside the scanned root: {candidate}")

    for protected in _protected_directories():
        if candidate == protected or candidate.is_relative_to(protected):
            raise UnsafePathError(f"refusing to delete a protected system directory: {candidate}")

    return candidate


def _send_to_trash(path: str) -> None:
    """Indirection so tests can replace the Recycle Bin call."""
    send2trash(path)


def move_to_recycle_bin(
    path: str | os.PathLike[str],
    *,
    scan_root: str | os.PathLike[str],
    confirmed: bool,
) -> Path:
    """Move one validated path to the Recycle Bin.

    ``confirmed`` must be true; callers obtain confirmation before calling.
    Any failure raises :class:`TrashError`; there is no permanent-delete
    fallback.
    """
    if not confirmed:
        raise ConfirmationRequired("moving to the Recycle Bin requires explicit confirmation")
    target = validate_deletable_path(path, scan_root)
    try:
        _send_to_trash(str(target))
    except Exception as exc:  # noqa: BLE001 - report the original failure
        raise TrashError(f"could not move {target} to the Recycle Bin: {exc}") from exc
    return target


_get_compressed_file_size: Any = None


def windows_allocated_size(path: str) -> int | None:
    """Return allocated size on Windows, or ``None`` on other platforms."""
    global _get_compressed_file_size

    if os.name != "nt":
        return None
    if _get_compressed_file_size is None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_size = kernel32.GetCompressedFileSizeW
        get_size.argtypes = [
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        get_size.restype = ctypes.c_ulong
        _get_compressed_file_size = get_size

    high = ctypes.c_ulong(0)
    ctypes.set_last_error(0)
    low = _get_compressed_file_size(path, ctypes.byref(high))
    if low == 0xFFFFFFFF:
        error = ctypes.get_last_error()
        if error:
            raise OSError(error, ctypes.FormatError(error), path)
    return (high.value << 32) | low
