"""Command-line interface for DiskScope space analysis."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from .cancellation import CancelToken
from .export import write_csv, write_json
from .models import FileKind, Progress, format_size
from .space import (
    ConfirmationRequired,
    InvalidScanRoot,
    TrashError,
    UnsafePathError,
    move_to_recycle_bin,
    scan,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="diskscope",
        description="Read-only disk space analysis. Deletion always uses the Recycle Bin.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="scan a directory and list largest files")
    scan_parser.add_argument("path", help="directory to scan")
    scan_parser.add_argument(
        "--top",
        type=int,
        default=50,
        help="largest files to retain (default 50)",
    )
    scan_parser.add_argument(
        "--kind",
        choices=[kind.value for kind in FileKind],
        help="show only this file kind in the largest-files list",
    )
    scan_parser.add_argument(
        "--allocated",
        action="store_true",
        help="measure allocated size on Windows (slower; off by default)",
    )
    scan_parser.add_argument("--json", metavar="FILE", help="write JSON report")
    scan_parser.add_argument("--csv", metavar="FILE", help="write CSV of largest files")
    scan_parser.add_argument(
        "--progress",
        action="store_true",
        help="print scan progress to stderr",
    )

    delete_parser = subparsers.add_parser(
        "delete",
        help="move one scanned path to the Recycle Bin after confirmation",
    )
    delete_parser.add_argument("path", help="file or directory to move to the Recycle Bin")
    delete_parser.add_argument("--root", required=True, help="scan root that contains the path")
    delete_parser.add_argument(
        "--yes",
        action="store_true",
        help="confirm the move explicitly (required when not interactive)",
    )
    return parser


def _progress_reporter(stream: TextIO) -> Callable[[Progress], None]:
    last_done = -1

    def report(update: Progress) -> None:
        nonlocal last_done
        if update.done == last_done and update.detail not in {"finished", "cancelled"}:
            return
        last_done = update.done
        stream.write(f"\r{update.done:,} entries  {update.detail[:80]}")
        stream.flush()
        if update.detail in {"finished", "cancelled"}:
            stream.write("\n")

    return report


def _cmd_scan(args: argparse.Namespace) -> int:
    token = CancelToken()
    progress = _progress_reporter(sys.stderr) if args.progress else None
    try:
        result = scan(
            args.path,
            cancel=token,
            progress=progress,
            top_n=args.top,
            allocated=args.allocated,
        )
    except KeyboardInterrupt:
        token.cancel()
        print("Cancelled.", file=sys.stderr)
        return 130
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Root: {result.root.path}")
    print(f"Logical size: {format_size(result.total_size)}")
    if result.allocated:
        if result.total_allocated_size is None:
            print("Allocated size: unavailable for some files")
        else:
            print(f"Allocated size: {format_size(result.total_allocated_size)}")
    print(f"Files: {result.file_count:,}  Folders: {result.directory_count:,}")
    if result.cancelled:
        print("Status: INCOMPLETE (cancelled)")
    elif not result.complete:
        print(f"Status: INCOMPLETE ({len(result.skipped):,} skipped)")
    else:
        print("Status: complete")

    kind_totals = sorted(result.kind_totals.items(), key=lambda item: (-item[1], item[0].value))
    if kind_totals:
        print("Kind totals:")
        for kind, size in kind_totals:
            print(f"  {kind.label:<11} {format_size(size):>10}")

    selected_kind = FileKind(args.kind) if args.kind else None
    heading = "Largest files" if selected_kind is None else f"Largest {selected_kind.label} files"
    shown = result.top_files(selected_kind, args.top)
    print(f"{heading} (top {args.top}):")
    for index, entry in enumerate(shown, start=1):
        print(f"  {index:>3}. {format_size(entry.size):>10}  {entry.path}")
    if not shown:
        print("  (none)")

    if result.skipped:
        print(f"Skipped: {len(result.skipped):,} item(s); see JSON report for details")

    try:
        if args.json:
            print(f"Wrote JSON: {write_json(result, args.json)}")
        if args.csv:
            print(f"Wrote CSV: {write_csv(result, args.csv)}")
    except OSError as exc:
        print(f"Error writing export: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_delete(args: argparse.Namespace) -> int:
    target = Path(args.path).expanduser()
    if not args.yes:
        if not sys.stdin.isatty():
            print(
                "Refusing to move to the Recycle Bin without --yes in a non-interactive session.",
                file=sys.stderr,
            )
            return 2
        answer = input(f"Move {target} to the Recycle Bin? [y/N] ")
        if answer.strip().casefold() not in {"y", "yes"}:
            print("Cancelled.", file=sys.stderr)
            return 2

    try:
        moved = move_to_recycle_bin(target, scan_root=args.root, confirmed=True)
    except (InvalidScanRoot, UnsafePathError, ConfirmationRequired, TrashError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Moved to Recycle Bin: {moved}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "scan":
        return _cmd_scan(args)
    if args.command == "delete":
        return _cmd_delete(args)
    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
