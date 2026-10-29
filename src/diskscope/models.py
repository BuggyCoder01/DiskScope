"""Shared immutable data contracts for DiskScope."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

Runner = Callable[[Sequence[str], float], object]
"""Injected command runner. It must return returncode/stdout/stderr data."""


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Normalized result of an injected command runner."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @classmethod
    def coerce(cls, value: object) -> CommandResult:
        """Accept CompletedProcess-like objects, CommandResult, or 2/3-tuples."""
        if isinstance(value, cls):
            return value
        if isinstance(value, tuple):
            if len(value) == 2:
                code, stdout = value
                stderr = ""
            elif len(value) >= 3:
                code, stdout, stderr = value[:3]
            else:
                raise TypeError("runner tuple must contain at least returncode and stdout")
            return cls(int(code), _as_text(stdout), _as_text(stderr))
        try:
            code = int(value.returncode)
            stdout = getattr(value, "stdout", None)
            if stdout is None:
                stdout = getattr(value, "output", "")
            stderr = getattr(value, "stderr", "")
            return cls(code, _as_text(stdout), _as_text(stderr))
        except (AttributeError, TypeError, ValueError) as exc:
            raise TypeError("runner must return returncode, stdout and stderr") from exc


def _as_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


@dataclass(frozen=True, slots=True)
class Progress:
    """A progress update delivered through a callback."""

    operation: str
    done: int
    total: int | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SkippedItem:
    """A path that could not be processed, with a human-readable reason."""

    path: str
    reason: str


class FileKind(StrEnum):
    """Coarse file categories used for type filtering."""

    DOCUMENT = "document"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    ARCHIVE = "archive"
    CODE = "code"
    EXECUTABLE = "executable"
    OTHER = "other"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ").title()


_EXTENSION_GROUPS: dict[FileKind, frozenset[str]] = {
    FileKind.DOCUMENT: frozenset(
        {
            ".csv",
            ".doc",
            ".docx",
            ".epub",
            ".md",
            ".odp",
            ".ods",
            ".odt",
            ".pdf",
            ".ppt",
            ".pptx",
            ".rtf",
            ".txt",
            ".xls",
            ".xlsx",
        }
    ),
    FileKind.IMAGE: frozenset(
        {
            ".bmp",
            ".gif",
            ".heic",
            ".jpeg",
            ".jpg",
            ".png",
            ".psd",
            ".raw",
            ".svg",
            ".tif",
            ".tiff",
            ".webp",
        }
    ),
    FileKind.VIDEO: frozenset(
        {
            ".avi",
            ".flv",
            ".m4v",
            ".mkv",
            ".mov",
            ".mp4",
            ".mpeg",
            ".mpg",
            ".ts",
            ".webm",
            ".wmv",
        }
    ),
    FileKind.AUDIO: frozenset(
        {
            ".aac",
            ".aiff",
            ".flac",
            ".m4a",
            ".mp3",
            ".ogg",
            ".opus",
            ".wav",
            ".wma",
        }
    ),
    FileKind.ARCHIVE: frozenset(
        {
            ".7z",
            ".bz2",
            ".cab",
            ".gz",
            ".iso",
            ".rar",
            ".tar",
            ".tgz",
            ".xz",
            ".zip",
            ".zst",
        }
    ),
    FileKind.CODE: frozenset(
        {
            ".bat",
            ".c",
            ".cmd",
            ".cpp",
            ".cs",
            ".css",
            ".go",
            ".h",
            ".hpp",
            ".html",
            ".ini",
            ".java",
            ".js",
            ".json",
            ".jsx",
            ".kt",
            ".php",
            ".ps1",
            ".py",
            ".rb",
            ".rs",
            ".scss",
            ".sh",
            ".sql",
            ".swift",
            ".toml",
            ".ts",
            ".tsx",
            ".xml",
            ".yaml",
            ".yml",
        }
    ),
    FileKind.EXECUTABLE: frozenset(
        {
            ".appx",
            ".com",
            ".dll",
            ".drv",
            ".exe",
            ".msi",
            ".msix",
            ".ocx",
            ".scr",
            ".sys",
        }
    ),
}

_EXTENSION_KINDS: dict[str, FileKind] = {
    extension: kind for kind, extensions in _EXTENSION_GROUPS.items() for extension in extensions
}


def classify_file(name: str) -> FileKind:
    """Classify a file by its extension. Unknown extensions are ``OTHER``."""
    return _EXTENSION_KINDS.get(Path(name).suffix.casefold(), FileKind.OTHER)


def format_size(size: int) -> str:
    """Format a byte count using binary units."""
    if size < 0:
        raise ValueError("size must be non-negative")
    if size < 1024:
        return f"{size} B"
    value = float(size)
    units = ("KiB", "MiB", "GiB", "TiB", "PiB", "EiB")
    for unit in units:
        value /= 1024
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f} {unit}"
    return f"{size} B"


@dataclass(frozen=True, slots=True)
class FileEntry:
    """A file that survived the top-N heap."""

    path: str
    size: int
    kind: FileKind = FileKind.OTHER
    allocated_size: int | None = None

    @property
    def name(self) -> str:
        return Path(self.path).name

    @property
    def extension(self) -> str:
        return Path(self.path).suffix.casefold()


@dataclass(slots=True, eq=False)
class DirNode:
    """A directory in the scanned tree.

    Sizes and counts are totals for the directory and all of its descendants.
    Only directories are stored individually; individual files are represented
    by the bounded top-N lists on :class:`SpaceScanResult`.
    """

    path: str
    name: str
    parent: DirNode | None = field(default=None, repr=False)
    size: int = 0
    allocated_size: int = 0
    file_count: int = 0
    dir_count: int = 0
    kind_sizes: dict[FileKind, int] = field(default_factory=dict)
    children: dict[str, DirNode] = field(default_factory=dict)

    def child(self, name: str) -> DirNode | None:
        """Find a direct child, matching case-insensitively."""
        direct = self.children.get(name)
        if direct is not None:
            return direct
        wanted = name.casefold()
        for key, node in self.children.items():
            if key.casefold() == wanted:
                return node
        return None

    def sorted_children(self) -> tuple[DirNode, ...]:
        """Children ordered by size descending, then casefolded path ascending."""
        return tuple(
            sorted(
                self.children.values(),
                key=lambda node: (-node.size, node.path.casefold(), node.path),
            )
        )

    def kind_size(self, kind: FileKind) -> int:
        """Total logical size of one file kind below this directory."""
        return self.kind_sizes.get(kind, 0)


@dataclass(frozen=True, slots=True)
class SpaceScanResult:
    """Result of a filesystem space scan.

    ``items`` is the bounded list of largest files, already ordered by size
    descending and casefolded path ascending. It is not a complete file list.
    A result is only ``complete`` when the scan finished and nothing was
    skipped.
    """

    root: DirNode
    items: tuple[FileEntry, ...]
    skipped: tuple[SkippedItem, ...]
    complete: bool
    cancelled: bool
    total_size: int
    total_allocated_size: int | None
    file_count: int
    directory_count: int
    allocated: bool
    top_n: int
    kind_totals: Mapping[FileKind, int] = field(default_factory=dict, repr=False, compare=False)
    _by_kind: Mapping[FileKind, tuple[FileEntry, ...]] = field(
        default_factory=dict, repr=False, compare=False
    )

    @property
    def largest(self) -> tuple[FileEntry, ...]:
        """Alias for :attr:`items`."""
        return self.items

    def top_files(
        self, kind: FileKind | str | None = None, limit: int | None = None
    ) -> tuple[FileEntry, ...]:
        """Return largest files overall or for one kind, optionally truncated."""
        source = self.items if kind is None else self._by_kind.get(FileKind(kind), ())
        if limit is None:
            return source
        if limit < 0:
            raise ValueError("limit must be non-negative")
        return source[:limit]


@dataclass(frozen=True, slots=True)
class DriveInfo:
    """A physical disk reported by ``Get-PhysicalDisk``.

    ``health_status`` and ``operational_status`` are stored exactly as reported;
    no health interpretation is performed in this module.
    """

    device_id: str
    friendly_name: str
    media_type: str | None = None
    bus_type: str | None = None
    size_bytes: int | None = None
    health_status: str | None = None
    operational_status: tuple[str, ...] = ()
    serial_number: str | None = None
    model: str | None = None
    firmware_version: str | None = None

    @property
    def name(self) -> str:
        return self.friendly_name

    @property
    def size(self) -> int | None:
        return self.size_bytes

    @property
    def health(self) -> str | None:
        return self.health_status

    @property
    def device_path(self) -> str:
        """Windows device path suitable for smartctl."""
        if not self.device_id:
            return ""
        if self.device_id.startswith(("\\\\.\\", "/dev/")):
            return self.device_id
        return rf"\\.\PhysicalDrive{self.device_id}"

    @property
    def path(self) -> str:
        return self.device_path


@dataclass(frozen=True, slots=True)
class SmartData(Mapping[str, Any]):
    """Parsed smartctl JSON with selected, non-interpreted fields.

    ``raw`` holds the complete JSON object. Missing values remain ``None`` or
    empty tuples; nothing is synthesized. Health interpretation belongs in the
    separate health module.
    """

    device: str
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)
    model_name: str | None = None
    serial_number: str | None = None
    firmware_version: str | None = None
    smart_supported: bool | None = None
    smart_enabled: bool | None = None
    health_passed: bool | None = None
    temperature_c: int | None = None
    power_on_hours: int | None = None
    attributes: tuple[Mapping[str, Any], ...] = ()

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.raw)

    def __len__(self) -> int:
        return len(self.raw)

    @property
    def payload(self) -> Mapping[str, Any]:
        return self.raw

    @property
    def smart_status_passed(self) -> bool | None:
        return self.health_passed

    @property
    def temperature(self) -> int | None:
        return self.temperature_c

    @property
    def messages(self) -> tuple[str, ...]:
        """smartctl's JSON messages, when present."""
        smartctl = self.raw.get("smartctl")
        if not isinstance(smartctl, Mapping):
            return ()
        messages = smartctl.get("messages")
        if not isinstance(messages, list):
            return ()
        result: list[str] = []
        for message in messages:
            text = message.get("string") if isinstance(message, Mapping) else message
            if isinstance(text, str) and text.strip():
                result.append(text.strip())
        return tuple(result)
