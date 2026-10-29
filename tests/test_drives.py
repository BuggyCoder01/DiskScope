"""Tests for read-only drive discovery. No real subprocess is started."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from diskscope.drives import DriveQueryError, list_drives, parse_drives_json
from diskscope.models import CommandResult, DriveInfo

FIXTURES = Path(__file__).parent / "fixtures"


class FakeRunner:
    """Records calls and returns or raises a canned result."""

    def __init__(
        self,
        result: object = None,
        error: BaseException | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv: Sequence[str], timeout: float) -> object:
        self.calls.append((list(argv), timeout))
        if self.error is not None:
            raise self.error
        return self.result


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_list_drives_parses_multiple_disks() -> None:
    runner = FakeRunner(CommandResult(0, _fixture("drives.json")))

    drives = list_drives(runner)

    assert len(drives) == 2
    nvme, hdd = drives

    assert isinstance(nvme, DriveInfo)
    assert nvme.device_id == "0"
    assert nvme.friendly_name == "Example NVMe"
    assert nvme.name == "Example NVMe"
    assert nvme.media_type == "SSD"
    assert nvme.bus_type == "NVMe"
    assert nvme.size_bytes == 512110190592
    assert nvme.size == 512110190592
    assert nvme.health_status == "Healthy"
    assert nvme.health == "Healthy"
    assert nvme.operational_status == ("OK",)
    assert nvme.serial_number == "TEST-0001"
    assert nvme.model == "Example NVMe"
    assert nvme.firmware_version == "1.0"
    assert nvme.device_path == r"\\.\PhysicalDrive0"
    assert nvme.path == nvme.device_path

    assert hdd.device_id == "1"
    assert hdd.friendly_name == "Example HDD"
    assert hdd.size_bytes == 1000204886016
    assert hdd.operational_status == ("OK", "Pred Fail")
    assert hdd.serial_number is None
    assert hdd.model is None
    assert hdd.firmware_version is None


def test_list_drives_uses_powershell_json() -> None:
    runner = FakeRunner(CommandResult(0, "[]"))

    list_drives(runner)

    assert len(runner.calls) == 1
    argv, timeout = runner.calls[0]
    assert argv[:4] == [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
    ]
    script = argv[4]
    assert "Get-PhysicalDisk" in script
    assert "ConvertTo-Json" in script
    assert "-Compress" in script
    assert timeout > 0


def test_list_drives_accepts_single_object() -> None:
    runner = FakeRunner((0, _fixture("drives_single.json")))

    drives = list_drives(runner)

    assert len(drives) == 1
    drive = drives[0]
    assert drive.device_id == "2"
    assert drive.friendly_name == "Single Disk"
    assert drive.media_type == "Unspecified"
    assert drive.bus_type == "USB"
    assert drive.size_bytes is None
    assert drive.health_status is None
    assert drive.operational_status == ()
    assert drive.serial_number is None
    assert drive.model is None
    assert drive.firmware_version is None
    assert drive.device_path == r"\\.\PhysicalDrive2"


@pytest.mark.parametrize("stdout", ["", "   \n", "null"])
def test_list_drives_empty_results(stdout: str) -> None:
    assert list_drives(FakeRunner((0, stdout))) == []


def test_list_drives_invalid_json_raises() -> None:
    with pytest.raises(DriveQueryError, match="invalid JSON"):
        list_drives(FakeRunner((0, "not json")))


def test_list_drives_nonzero_exit_raises_with_detail() -> None:
    runner = FakeRunner(CommandResult(1, "", "Access denied"))

    with pytest.raises(DriveQueryError) as caught:
        list_drives(runner)

    message = str(caught.value)
    assert "code 1" in message
    assert "Access denied" in message


def test_list_drives_missing_powershell() -> None:
    with pytest.raises(DriveQueryError, match="PowerShell not found"):
        list_drives(FakeRunner(error=FileNotFoundError("powershell.exe")))


def test_list_drives_timeout() -> None:
    error = subprocess.TimeoutExpired(["powershell.exe"], 30.0)

    with pytest.raises(DriveQueryError, match="timed out"):
        list_drives(FakeRunner(error=error))


def test_list_drives_permission_denied() -> None:
    with pytest.raises(DriveQueryError, match="permission denied"):
        list_drives(FakeRunner(error=PermissionError("denied")))


@pytest.mark.parametrize(
    "payload",
    ["42", '["not an object"]', '[{"DeviceId": "0"}, 7]'],
)
def test_list_drives_rejects_malformed_items(payload: str) -> None:
    with pytest.raises(DriveQueryError):
        list_drives(FakeRunner((0, payload)))


def test_list_drives_invalid_runner_result() -> None:
    with pytest.raises(DriveQueryError, match="invalid runner result"):
        list_drives(FakeRunner(object()))


def test_parse_drives_json_skips_null_items() -> None:
    drives = parse_drives_json('[null, {"DeviceId": "4", "FriendlyName": "Disk"}]')

    assert [drive.device_id for drive in drives] == ["4"]


def test_parse_drives_json_does_not_invent_missing_values() -> None:
    drives = parse_drives_json(
        '[{"DeviceId": "5", "FriendlyName": "Disk", "Size": "not-a-number"}]'
    )

    drive = drives[0]
    assert drive.size_bytes is None
    assert drive.media_type is None
    assert drive.health_status is None
    assert drive.operational_status == ()
    assert drive.serial_number is None
    assert drive.model is None
    assert drive.firmware_version is None
