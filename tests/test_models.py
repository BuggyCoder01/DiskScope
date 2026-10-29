from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from diskscope.models import FileKind, Progress, classify_file, format_size


def test_progress_is_frozen() -> None:
    update = Progress("scan", 10, None, "detail")
    with pytest.raises(FrozenInstanceError):
        update.done = 11  # type: ignore[misc]


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("report.PDF", FileKind.DOCUMENT),
        ("photo.jpg", FileKind.IMAGE),
        ("movie.mkv", FileKind.VIDEO),
        ("song.flac", FileKind.AUDIO),
        ("backup.7z", FileKind.ARCHIVE),
        ("script.py", FileKind.CODE),
        ("setup.exe", FileKind.EXECUTABLE),
        ("README", FileKind.OTHER),
        ("archive.unknown", FileKind.OTHER),
    ],
)
def test_classify_file(name: str, kind: FileKind) -> None:
    assert classify_file(name) is kind


def test_format_size() -> None:
    assert format_size(0) == "0 B"
    assert format_size(1023) == "1023 B"
    assert format_size(1024) == "1.0 KiB"
    assert format_size(1536) == "1.5 KiB"
    with pytest.raises(ValueError):
        format_size(-1)
