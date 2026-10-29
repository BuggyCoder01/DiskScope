from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from diskscope.export import write_csv, write_json
from diskscope.space import scan


def test_write_json_roundtrip(sample_tree: Path, tmp_path: Path) -> None:
    result = scan(sample_tree, top_n=3)
    destination = tmp_path / "report.json"

    written = write_json(result, destination)

    assert written == destination
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload["root"] == str(sample_tree.resolve())
    assert payload["total_size"] == 2125
    assert payload["file_count"] == 7
    assert payload["complete"] is True
    assert payload["skipped"] == []
    assert len(payload["largest_files"]) == 3
    assert payload["largest_files"][0]["path"].endswith("movie.mp4")
    assert payload["tree"]["path"] == str(sample_tree.resolve())
    assert payload["tree"]["allocated_size"] is None
    assert "children" in payload["tree"]


def test_write_json_preserves_unicode(sample_tree: Path, tmp_path: Path) -> None:
    (sample_tree / "café.txt").write_bytes(b"x")
    result = scan(sample_tree, top_n=20)
    destination = tmp_path / "unicode.json"

    write_json(result, destination)

    text = destination.read_text(encoding="utf-8")
    assert "café.txt" in text


def test_write_csv_largest_files(sample_tree: Path, tmp_path: Path) -> None:
    result = scan(sample_tree, top_n=3)
    destination = tmp_path / "largest.csv"

    written = write_csv(result, destination)

    assert written == destination
    with destination.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.reader(handle))
    assert rows[0] == ["path", "size", "allocated_size", "kind"]
    assert len(rows) == 4
    assert rows[1][0].endswith("movie.mp4")
    assert rows[1][1] == "1000"
    assert rows[1][2] == ""
    assert rows[1][3] == "video"


def test_export_missing_directory_is_reported(sample_tree: Path, tmp_path: Path) -> None:
    result = scan(sample_tree, top_n=1)
    destination = tmp_path / "missing" / "report.json"

    with pytest.raises(FileNotFoundError, match="export directory"):
        write_json(result, destination)


def test_export_does_not_leave_temporary_file(sample_tree: Path, tmp_path: Path) -> None:
    result = scan(sample_tree, top_n=1)
    destination = tmp_path / "report.json"

    write_json(result, destination)

    assert list(tmp_path.glob(".report.json.*.tmp")) == []
