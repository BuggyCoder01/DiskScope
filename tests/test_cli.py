from __future__ import annotations

import io
from pathlib import Path

import pytest

from diskscope import cli, space
from diskscope.cli import main


def test_cli_scan_prints_summary(sample_tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["scan", str(sample_tree), "--top", "2"])

    captured = capsys.readouterr()
    assert code == 0
    assert "Status: complete" in captured.out
    assert "Logical size:" in captured.out
    assert "movie.mp4" in captured.out
    assert "report.pdf" not in captured.out


def test_cli_scan_kind_filter(sample_tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["scan", str(sample_tree), "--kind", "image", "--top", "10"])

    captured = capsys.readouterr()
    assert code == 0
    assert "Largest Image files" in captured.out
    assert "photo.JPG" in captured.out
    assert "movie.mp4" not in captured.out
    assert "report.pdf" not in captured.out


def test_cli_scan_kind_filter_without_matches(
    sample_tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["scan", str(sample_tree), "--kind", "executable", "--top", "10"])

    captured = capsys.readouterr()
    assert code == 0
    assert "Largest Executable files" in captured.out
    assert "(none)" in captured.out


def test_cli_scan_writes_exports(
    sample_tree: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    json_path = tmp_path / "report.json"
    csv_path = tmp_path / "report.csv"

    code = main(
        [
            "scan",
            str(sample_tree),
            "--top",
            "3",
            "--json",
            str(json_path),
            "--csv",
            str(csv_path),
        ]
    )

    captured = capsys.readouterr()
    assert code == 0
    assert json_path.exists()
    assert csv_path.exists()
    assert "Wrote JSON" in captured.out
    assert "Wrote CSV" in captured.out


def test_cli_scan_rejects_missing_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["scan", str(tmp_path / "missing")])

    captured = capsys.readouterr()
    assert code == 1
    assert "does not exist" in captured.err


def test_cli_scan_rejects_negative_top(
    sample_tree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["scan", str(sample_tree), "--top", "-1"])

    captured = capsys.readouterr()
    assert code == 1
    assert "top_n" in captured.err


def test_cli_delete_refuses_without_confirmation_in_noninteractive_session(
    sample_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(space, "_send_to_trash", calls.append)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO())
    target = sample_tree / "alpha.bin"

    code = main(["delete", str(target), "--root", str(sample_tree)])

    captured = capsys.readouterr()
    assert code == 2
    assert "without --yes" in captured.err
    assert calls == []
    assert target.exists()


def test_cli_delete_with_yes(
    sample_tree: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(space, "_send_to_trash", calls.append)
    target = sample_tree / "alpha.bin"

    code = main(["delete", str(target), "--root", str(sample_tree), "--yes"])

    captured = capsys.readouterr()
    assert code == 0
    assert "Moved to Recycle Bin" in captured.out
    assert calls == [str(target.resolve())]


def test_cli_delete_rejects_outside_root(
    sample_tree: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(space, "_send_to_trash", calls.append)
    outside = tmp_path.parent / f"{tmp_path.name}-cli-outside.bin"
    outside.write_bytes(b"outside")

    code = main(["delete", str(outside), "--root", str(sample_tree), "--yes"])

    captured = capsys.readouterr()
    assert code == 1
    assert "outside" in captured.err
    assert calls == []
    assert outside.exists()
