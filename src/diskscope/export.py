"""JSON and CSV export for space-scan results."""

from __future__ import annotations

import csv
import json
import os
import tempfile
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import TextIO

from .models import DirNode, SpaceScanResult


def write_json(result: SpaceScanResult, path: str | os.PathLike[str]) -> Path:
    """Write a scan summary, largest files, skipped items and tree as UTF-8 JSON."""
    destination = Path(path)

    def write(handle: TextIO) -> None:
        json.dump(_result_payload(result), handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    _atomic_write(destination, write)
    return destination


def write_csv(result: SpaceScanResult, path: str | os.PathLike[str]) -> Path:
    """Write the largest-file list as UTF-8 CSV."""
    destination = Path(path)

    def write(handle: TextIO) -> None:
        writer = csv.writer(handle)
        writer.writerow(("path", "size", "allocated_size", "kind"))
        for entry in result.items:
            writer.writerow(
                (
                    entry.path,
                    entry.size,
                    "" if entry.allocated_size is None else entry.allocated_size,
                    entry.kind.value,
                )
            )

    _atomic_write(destination, write)
    return destination


def _result_payload(result: SpaceScanResult) -> dict[str, object]:
    return {
        "root": result.root.path,
        "complete": result.complete,
        "cancelled": result.cancelled,
        "allocated": result.allocated,
        "total_size": result.total_size,
        "total_allocated_size": result.total_allocated_size,
        "file_count": result.file_count,
        "directory_count": result.directory_count,
        "kind_totals": {
            kind.value: size
            for kind, size in sorted(
                result.kind_totals.items(), key=lambda item: (-item[1], item[0].value)
            )
        },
        "largest_files": [
            {
                "path": entry.path,
                "size": entry.size,
                "allocated_size": entry.allocated_size,
                "kind": entry.kind.value,
            }
            for entry in result.items
        ],
        "skipped": [{"path": item.path, "reason": item.reason} for item in result.skipped],
        "tree": _node_payload(
            result.root,
            allocated=result.allocated and result.total_allocated_size is not None,
        ),
    }


def _node_payload(node: DirNode, *, allocated: bool) -> dict[str, object]:
    return {
        "path": node.path,
        "name": node.name,
        "size": node.size,
        "allocated_size": node.allocated_size if allocated else None,
        "file_count": node.file_count,
        "directory_count": node.dir_count,
        "kind_sizes": {
            kind.value: size
            for kind, size in sorted(node.kind_sizes.items(), key=lambda item: item[0].value)
        },
        "children": [_node_payload(child, allocated=allocated) for child in node.sorted_children()],
    }


def _atomic_write(destination: Path, writer: Callable[[TextIO], None]) -> None:
    parent = destination.parent
    if not parent.exists():
        raise FileNotFoundError(f"export directory does not exist: {parent}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer(handle)
        os.replace(temporary_name, destination)
    except BaseException:
        # Clean up only the export temporary file. User files are never deleted
        # here; Recycle Bin moves go through send2trash exclusively.
        with suppress(OSError):
            os.unlink(temporary_name)
        raise
