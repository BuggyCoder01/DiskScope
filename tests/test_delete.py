from __future__ import annotations

from pathlib import Path

import pytest

from diskscope import space
from diskscope.space import (
    ConfirmationRequired,
    TrashError,
    UnsafePathError,
    move_to_recycle_bin,
)


@pytest.fixture
def trash_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def fake_trash(path: str) -> None:
        calls.append(path)

    monkeypatch.setattr(space, "_send_to_trash", fake_trash)
    return calls


def test_delete_requires_explicit_confirmation(sample_tree: Path, trash_calls: list[str]) -> None:
    target = sample_tree / "alpha.bin"

    with pytest.raises(ConfirmationRequired):
        move_to_recycle_bin(target, scan_root=sample_tree, confirmed=False)

    assert trash_calls == []
    assert target.exists()


def test_delete_moves_valid_file(sample_tree: Path, trash_calls: list[str]) -> None:
    target = sample_tree / "alpha.bin"

    moved = move_to_recycle_bin(target, scan_root=sample_tree, confirmed=True)

    assert moved == target.resolve()
    assert trash_calls == [str(target.resolve())]


def test_delete_allows_subdirectory(sample_tree: Path, trash_calls: list[str]) -> None:
    target = sample_tree / "empty"

    moved = move_to_recycle_bin(target, scan_root=sample_tree, confirmed=True)

    assert moved == target.resolve()
    assert trash_calls == [str(target.resolve())]


def test_delete_rejects_scan_root(sample_tree: Path, trash_calls: list[str]) -> None:
    with pytest.raises(UnsafePathError, match="scan root"):
        move_to_recycle_bin(sample_tree, scan_root=sample_tree, confirmed=True)
    assert trash_calls == []


def test_delete_rejects_outside_root(
    sample_tree: Path, tmp_path: Path, trash_calls: list[str]
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.bin"
    outside.write_bytes(b"outside")

    with pytest.raises(UnsafePathError, match="outside"):
        move_to_recycle_bin(outside, scan_root=sample_tree, confirmed=True)

    assert trash_calls == []
    assert outside.exists()


def test_delete_rejects_traversal_outside_root(
    sample_tree: Path, tmp_path: Path, trash_calls: list[str]
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-traversal.bin"
    outside.write_bytes(b"outside")

    with pytest.raises(UnsafePathError):
        move_to_recycle_bin(
            sample_tree / ".." / outside.name,
            scan_root=sample_tree,
            confirmed=True,
        )

    assert trash_calls == []
    assert outside.exists()


def test_delete_rejects_drive_root(sample_tree: Path, trash_calls: list[str]) -> None:
    drive_root = Path(sample_tree.anchor)

    with pytest.raises(UnsafePathError):
        move_to_recycle_bin(drive_root, scan_root=sample_tree, confirmed=True)

    assert trash_calls == []


def test_delete_rejects_protected_directory(
    sample_tree: Path, monkeypatch: pytest.MonkeyPatch, trash_calls: list[str]
) -> None:
    protected = sample_tree / "docs"
    monkeypatch.setattr(space, "_protected_directories", lambda: (protected,))
    target = protected / "report.pdf"

    with pytest.raises(UnsafePathError, match="protected"):
        move_to_recycle_bin(target, scan_root=sample_tree, confirmed=True)

    assert trash_calls == []
    assert target.exists()


def test_delete_rejects_symlink(sample_tree: Path, trash_calls: list[str]) -> None:
    target = sample_tree / "alpha.bin"
    link = sample_tree / "alpha-link.bin"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(UnsafePathError, match="reparse"):
        move_to_recycle_bin(link, scan_root=sample_tree, confirmed=True)

    assert trash_calls == []
    assert target.exists()


def test_trash_failure_is_reported_and_never_permanent(
    sample_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = sample_tree / "alpha.bin"

    def failing_trash(path: str) -> None:
        raise OSError("file is locked")

    def fail_unlink(*args: object, **kwargs: object) -> None:
        raise AssertionError("permanent delete must not be attempted")

    monkeypatch.setattr(space, "_send_to_trash", failing_trash)
    monkeypatch.setattr(space.os, "unlink", fail_unlink)

    with pytest.raises(TrashError, match="locked"):
        move_to_recycle_bin(target, scan_root=sample_tree, confirmed=True)

    assert target.exists()


def test_delete_rejects_missing_path(sample_tree: Path, trash_calls: list[str]) -> None:
    with pytest.raises(UnsafePathError, match="does not exist"):
        move_to_recycle_bin(
            sample_tree / "missing.bin",
            scan_root=sample_tree,
            confirmed=True,
        )
    assert trash_calls == []


def test_delete_rejects_reparse_attribute(
    sample_tree: Path, monkeypatch: pytest.MonkeyPatch, trash_calls: list[str]
) -> None:
    from types import SimpleNamespace

    target = sample_tree / "alpha.bin"
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda self: SimpleNamespace(
            st_mode=0o100644,
            st_file_attributes=space._FILE_ATTRIBUTE_REPARSE_POINT,
        ),
    )

    with pytest.raises(UnsafePathError, match="reparse"):
        move_to_recycle_bin(target, scan_root=sample_tree, confirmed=True)

    assert trash_calls == []
