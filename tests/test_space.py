from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from diskscope import space
from diskscope.cancellation import CancelToken
from diskscope.models import FileKind, Progress
from diskscope.space import InvalidScanRoot, UnsafePathError, children_for, node_for_path


class _FakeEntry:
    def __init__(
        self,
        path: Path,
        *,
        mode: int = stat.S_IFREG | 0o644,
        attributes: int = 0,
        size: int = 0,
        symlink: bool = False,
        junction: bool = False,
        error: OSError | None = None,
    ) -> None:
        self.path = str(path)
        self.name = path.name
        self._mode = mode
        self._attributes = attributes
        self._size = size
        self._symlink = symlink
        self._junction = junction
        self._error = error

    def is_symlink(self) -> bool:
        return self._symlink

    def is_junction(self) -> bool:
        return self._junction

    def stat(self, follow_symlinks: bool = True) -> object:
        if self._error is not None:
            raise self._error
        return SimpleNamespace(
            st_mode=self._mode,
            st_file_attributes=self._attributes,
            st_size=self._size,
        )


class _FakeScandir:
    def __init__(self, entries: list[_FakeEntry]) -> None:
        self._entries = entries
        self.closed = False

    def __enter__(self) -> object:
        return iter(self._entries)

    def __exit__(self, *args: object) -> bool:
        self.closed = True
        return False


def test_scan_totals_and_top_n(sample_tree: Path) -> None:
    result = space.scan(sample_tree, top_n=3)

    assert result.complete is True
    assert result.cancelled is False
    assert result.total_size == 2125
    assert result.file_count == 7
    assert result.directory_count == 5
    assert result.total_allocated_size is None
    assert [entry.name for entry in result.items] == [
        "movie.mp4",
        "archive.zip",
        "report.pdf",
    ]
    assert result.kind_totals[FileKind.VIDEO] == 1000
    assert result.kind_totals[FileKind.IMAGE] == 200
    assert [entry.name for entry in result.top_files(FileKind.VIDEO)] == ["movie.mp4"]
    assert [entry.name for entry in result.top_files(FileKind.IMAGE)] == ["photo.JPG"]
    assert result.top_files("video", 1)[0].name == "movie.mp4"
    assert result.root.kind_size(FileKind.VIDEO) == 1000


def test_scan_is_deterministic_for_ties(tmp_path: Path) -> None:
    (tmp_path / "B.bin").write_bytes(b"x" * 10)
    (tmp_path / "a.bin").write_bytes(b"x" * 10)

    result = space.scan(tmp_path, top_n=5)

    assert [entry.name for entry in result.items] == ["a.bin", "B.bin"]


def test_top_n_zero_keeps_totals(sample_tree: Path) -> None:
    result = space.scan(sample_tree, top_n=0)

    assert result.items == ()
    assert result.top_files(FileKind.VIDEO) == ()
    assert result.total_size == 2125
    assert result.file_count == 7


def test_scan_rejects_missing_or_file_root(tmp_path: Path, sample_tree: Path) -> None:
    with pytest.raises(InvalidScanRoot):
        space.scan(tmp_path / "missing")
    with pytest.raises(InvalidScanRoot):
        space.scan(sample_tree / "alpha.bin")


def test_permission_error_is_skipped(sample_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blocked = sample_tree / "media"
    real_scandir = os.scandir

    def fake_scandir(path: str | os.PathLike[str]) -> object:
        if Path(path) == blocked:
            raise PermissionError(13, "denied", str(path))
        return real_scandir(path)

    monkeypatch.setattr(space.os, "scandir", fake_scandir)

    result = space.scan(sample_tree, top_n=10)

    assert result.complete is False
    assert result.file_count == 5
    assert result.total_size == 925
    assert any(item.path == str(blocked) and "permission" in item.reason for item in result.skipped)


def test_vanished_entry_is_skipped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ghost = tmp_path / "ghost.txt"
    entry = _FakeEntry(ghost, error=FileNotFoundError(2, "gone", str(ghost)))
    monkeypatch.setattr(space.os, "scandir", lambda path: _FakeScandir([entry]))

    result = space.scan(tmp_path)

    assert result.complete is False
    assert result.file_count == 0
    assert result.skipped[0].path == str(ghost)
    assert "vanished" in result.skipped[0].reason


def test_reparse_points_are_not_followed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    symlink = _FakeEntry(tmp_path / "link", mode=stat.S_IFDIR | 0o755, symlink=True)
    junction = _FakeEntry(tmp_path / "junction", mode=stat.S_IFDIR | 0o755, junction=True)
    reparse = _FakeEntry(
        tmp_path / "pinned.bin",
        mode=stat.S_IFREG | 0o644,
        attributes=space._FILE_ATTRIBUTE_REPARSE_POINT,
        size=10,
    )
    monkeypatch.setattr(
        space.os,
        "scandir",
        lambda path: _FakeScandir([symlink, junction, reparse]),
    )

    result = space.scan(tmp_path)

    assert result.file_count == 0
    assert result.total_size == 0
    assert result.complete is False
    reasons = {Path(item.path).name: item.reason for item in result.skipped}
    assert "symbolic link" in reasons["link"]
    assert "junction" in reasons["junction"]
    assert "reparse point" in reasons["pinned.bin"]


def test_allocated_size_uses_injected_interface(sample_tree: Path) -> None:
    def fake_allocated(path: str) -> int:
        return Path(path).stat().st_size * 2

    result = space.scan(sample_tree, allocated=True, allocated_size=fake_allocated, top_n=10)

    assert result.allocated is True
    assert result.total_allocated_size == 4250
    assert all(entry.allocated_size == entry.size * 2 for entry in result.items)


def test_allocated_failure_is_reported(sample_tree: Path) -> None:
    def fake_allocated(path: str) -> int:
        if path.endswith("movie.mp4"):
            raise PermissionError(13, "denied", path)
        return 10

    result = space.scan(sample_tree, allocated=True, allocated_size=fake_allocated, top_n=10)

    assert result.complete is False
    assert result.total_allocated_size is None
    movie = next(entry for entry in result.items if entry.name == "movie.mp4")
    assert movie.allocated_size is None
    assert any("allocated size unavailable" in item.reason for item in result.skipped)


def test_cancel_before_scan(sample_tree: Path) -> None:
    token = CancelToken()
    token.cancel()

    result = space.scan(sample_tree, cancel=token)

    assert result.cancelled is True
    assert result.complete is False
    assert result.file_count == 0
    assert result.directory_count == 1
    assert result.items == ()


def test_cancel_mid_scan(sample_tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(space, "_PROGRESS_EVERY", 1)
    token = CancelToken()

    def on_progress(update: Progress) -> None:
        if update.done >= 2:
            token.cancel()

    result = space.scan(sample_tree, cancel=token, progress=on_progress, top_n=10)

    assert result.cancelled is True
    assert result.complete is False
    assert 0 < result.file_count < 7


def test_cancel_closes_scandir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    entries = [_FakeEntry(tmp_path / f"file-{index}.bin", size=1) for index in range(5)]
    tracker = _FakeScandir(entries)
    monkeypatch.setattr(space.os, "scandir", lambda path: tracker)
    monkeypatch.setattr(space, "_PROGRESS_EVERY", 1)
    token = CancelToken()

    def on_progress(update: Progress) -> None:
        if update.done >= 1:
            token.cancel()

    result = space.scan(tmp_path, cancel=token, progress=on_progress)

    assert result.cancelled is True
    assert tracker.closed is True


def test_progress_reports_start_and_completion(sample_tree: Path) -> None:
    updates: list[object] = []
    result = space.scan(sample_tree, progress=updates.append, top_n=1)

    assert updates[0].operation == "scan"
    assert updates[0].done == 0
    assert updates[-1].detail == "finished"
    assert updates[-1].done > 0
    assert result.complete is True


def test_hardlinks_are_counted_per_name(tmp_path: Path) -> None:
    source = tmp_path / "one.bin"
    source.write_bytes(b"x" * 32)
    try:
        os.link(source, tmp_path / "two.bin")
    except OSError as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    result = space.scan(tmp_path, top_n=10)

    assert result.file_count == 2
    assert result.total_size == 64


def test_node_lookup_and_kind_filter(sample_tree: Path) -> None:
    result = space.scan(sample_tree, top_n=10)

    docs = node_for_path(result, "docs")
    assert docs.size == 425
    assert docs.file_count == 3
    assert [node.name for node in children_for(result.root, FileKind.VIDEO)] == ["media"]
    assert [node.name for node in children_for(result.root, FileKind.DOCUMENT)] == ["docs"]
    with pytest.raises(UnsafePathError):
        node_for_path(result, sample_tree.parent)
    with pytest.raises(KeyError):
        node_for_path(result, "missing")


def test_allocated_requires_windows_without_injected_interface(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Windows has a real allocated-size implementation")
    with pytest.raises(ValueError, match="only available on Windows"):
        space.scan(tmp_path, allocated=True)
