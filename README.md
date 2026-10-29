# DiskScope

DiskScope is a Windows-first, read-only disk-space analyzer. The current
milestone covers filesystem space analysis only. Drive information, SMART
health and surface scans are separate modules and are not implemented yet.

## Install

```powershell
python -m pip install -e ".[dev]"
```

## Command line

```powershell
diskscope scan C:\Users\me --top 100
diskscope scan C:\Users\me --kind video --json report.json --csv largest.csv
diskscope scan D:\archive --allocated --progress
diskscope delete "C:\Users\me\Downloads\old.iso" --root "C:\Users\me" --yes
```

`delete` asks for confirmation unless `--yes` is supplied. In a non-interactive
session it refuses to proceed without `--yes`.

## Desktop interface

```powershell
diskscope-gui
```

The Qt window runs scans in a `QThread`, shows the directory tree and largest
files, filters the largest-file list by type, supports cancellation, and moves
confirmed files to the Recycle Bin.

## Size semantics

- **Logical size is the default.** It is the sum of `st_size` for each directory
  entry. Hardlinks are counted once per name, not once per inode: two hardlinked
  names contribute twice to totals and to the largest-file list.
- **Allocated size is opt-in.** On Windows it uses `GetCompressedFileSizeW`
  through `ctypes`; it is slower and is labelled separately in CLI and GUI output.
  On other platforms it is available only through an injected allocator.
- **Links and reparse points are never followed.** Symbolic links, junctions and
  other reparse points are reported in `skipped` and do not contribute to totals.
- **Partial results are never marked complete.** Cancellation or any skipped
  entry sets `complete=False`. Every result carries `items`, `skipped` and
  `complete`.

## Safety

- Scans are read-only and never trigger hardware operations.
- Deletion only uses `send2trash`. A failure raises and is reported; there is no
  fallback to `unlink`, `rmtree` or permanent deletion.
- Before deletion, paths are resolved and must stay inside the scan root. Drive
  roots, the scan root itself, Windows/Program Files directories and reparse
  points are refused.
- Tests never touch real disks: scans use `tmp_path`, and subprocess/Recycle Bin
  calls are faked.

## Layout

```text
src/diskscope/
  models.py        shared Progress/SkippedItem/FileEntry/DirNode/result models
  cancellation.py  CancelToken
  space.py         iterative scan, browse helpers, guarded Recycle Bin move
  export.py        JSON and CSV reports
  cli.py           command-line interface
  gui/             PySide6 interface (core never imports PySide6)
tests/             unit tests and recorded fixtures
```

## Checks

```powershell
ruff check --fix .
ruff format .
pytest -q -x --tb=short
```
