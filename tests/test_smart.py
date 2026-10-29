"""Tests for read-only SMART access. No real smartctl process is started."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from diskscope.models import CommandResult
from diskscope.smart import SmartResult, read_smart

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


def test_read_smart_parses_json_and_invokes_smartctl_json() -> None:
    runner = FakeRunner(CommandResult(0, _fixture("smart_ok.json")))

    result = read_smart(r"\\.\PhysicalDrive0", runner, 5.0)

    assert isinstance(result, SmartResult)
    assert runner.calls == [(["smartctl", "-j", "-a", r"\\.\PhysicalDrive0"], 5.0)]
    assert result.device == r"\\.\PhysicalDrive0"
    assert result.available is True
    assert result.ok is True
    assert result.exit_code == 0
    assert result.execution_errors == ()
    assert result.device_conditions == ()
    assert result.execution_error_bits == ()
    assert result.device_condition_bits == ()
    assert result.error is None
    assert result.reasons == ()

    data = result.data
    assert data is not None
    assert result.smart is data
    assert result.smart_data is data
    assert data.model_name == "Example SSD"
    assert data.serial_number == "TEST-SMART-1"
    assert data.firmware_version == "1.2.3"
    assert data.smart_supported is True
    assert data.smart_enabled is True
    assert data.health_passed is True
    assert data.smart_status_passed is True
    assert data.temperature_c == 35
    assert data.temperature == 35
    assert data.power_on_hours == 1234
    assert len(data.attributes) == 2
    assert data.attributes[0]["name"] == "Reallocated_Sector_Ct"
    assert data.messages == ()
    assert data["model_name"] == "Example SSD"
    assert "smartctl" in data
    assert result.payload == data.raw


def test_read_smart_parses_nvme_log_fields() -> None:
    runner = FakeRunner((0, _fixture("smart_nvme.json")))

    result = read_smart("/dev/nvme0", runner, 2.0)

    data = result.data
    assert data is not None
    assert result.ok is True
    assert data.model_name == "Example NVMe"
    assert data.serial_number == "NVME-0001"
    assert data.firmware_version == "2.0"
    assert data.health_passed is True
    assert data.temperature_c == 41
    assert data.power_on_hours == 4321
    assert data.attributes == ()
    assert data.raw["nvme_smart_health_information_log"]["temperature"] == 41


def test_read_smart_mixed_exit_bits_keep_json() -> None:
    runner = FakeRunner((25, _fixture("smart_ok.json")))

    result = read_smart("/dev/sda", runner, 2.0)

    assert result.available is True
    assert result.ok is False
    assert result.data is not None
    assert result.data.model_name == "Example SSD"
    assert result.exit_code == 25
    assert result.execution_error_bits == (0,)
    assert result.device_condition_bits == (3, 4)
    assert result.execution_errors == ("command line did not parse",)
    assert len(result.device_conditions) == 2
    assert "DISK FAILING" in result.device_conditions[0]
    assert result.reasons == result.execution_errors + result.device_conditions


def test_read_smart_device_condition_only() -> None:
    runner = FakeRunner((8, _fixture("smart_ok.json")))

    result = read_smart("/dev/sda", runner, 2.0)

    assert result.available is True
    assert result.ok is False
    assert result.execution_errors == ()
    assert result.execution_error_bits == ()
    assert result.device_condition_bits == (3,)
    assert result.device_conditions == ("SMART status check returned DISK FAILING",)


def test_read_smart_device_open_failure_keeps_json_messages() -> None:
    runner = FakeRunner((2, _fixture("smart_device_open_failed.json")))

    result = read_smart("/dev/sdb", runner, 2.0)

    assert result.available is True
    assert result.data is not None
    assert result.exit_code == 2
    assert result.execution_error_bits == (1,)
    assert result.execution_errors == ("device open failed or device did not identify",)
    assert result.device_conditions == ()
    assert "Permission denied" in result.messages[0]
    assert result.data.model_name == "Example USB"


def test_read_smart_invalid_json_is_reported() -> None:
    runner = FakeRunner((0, "not json"))

    result = read_smart("/dev/sda", runner, 2.0)

    assert result.available is False
    assert result.ok is False
    assert result.data is None
    assert result.exit_code == 0
    assert any("invalid JSON" in error for error in result.execution_errors)
    assert result.messages == ()
    assert result.payload is None


def test_read_smart_invalid_json_keeps_exit_classification() -> None:
    runner = FakeRunner((8, "not json"))

    result = read_smart("/dev/sda", runner, 2.0)

    assert result.available is False
    assert result.execution_error_bits == ()
    assert result.device_condition_bits == (3,)
    assert result.device_conditions == ("SMART status check returned DISK FAILING",)
    assert any("invalid JSON" in error for error in result.execution_errors)


def test_read_smart_empty_output_is_reported() -> None:
    result = read_smart("/dev/sda", FakeRunner((0, "  ")), 2.0)

    assert result.available is False
    assert result.data is None
    assert result.execution_errors == ("smartctl returned no JSON output",)


def test_read_smart_missing_binary() -> None:
    result = read_smart("/dev/sda", FakeRunner(error=FileNotFoundError("smartctl")), 2.0)

    assert result.available is False
    assert result.execution_errors == ("smartctl not found",)
    assert result.exit_code is None


def test_read_smart_timeout() -> None:
    error = subprocess.TimeoutExpired(["smartctl", "-j", "-a", "/dev/sda"], 2.0, stderr="late")

    result = read_smart("/dev/sda", FakeRunner(error=error), 2.0)

    assert result.available is False
    assert result.execution_errors == ("smartctl timed out after 2s",)
    assert result.stderr == "late"


def test_read_smart_permission_denied() -> None:
    error = PermissionError(13, "denied")

    result = read_smart("/dev/sda", FakeRunner(error=error), 2.0)

    assert result.available is False
    assert result.execution_errors == ("permission denied",)


def test_read_smart_disconnected_device() -> None:
    error = ConnectionResetError("disconnected")

    result = read_smart("/dev/sda", FakeRunner(error=error), 2.0)

    assert result.available is False
    assert result.execution_errors == ("device disconnected: disconnected",)


def test_read_smart_windows_device_not_connected() -> None:
    error = OSError(22, "The device is not connected.", None, 1167)

    result = read_smart(r"\\.\PhysicalDrive9", FakeRunner(error=error), 2.0)

    assert result.available is False
    assert result.execution_errors[0].startswith("device unavailable:")


def test_read_smart_called_process_error_still_parses_output() -> None:
    error = subprocess.CalledProcessError(
        8,
        ["smartctl", "-j", "-a", "/dev/sda"],
        output=_fixture("smart_ok.json"),
        stderr="",
    )

    result = read_smart("/dev/sda", FakeRunner(error=error), 2.0)

    assert result.available is True
    assert result.data is not None
    assert result.device_condition_bits == (3,)
    assert result.execution_errors == ()


def test_read_smart_reports_unrecognised_high_bit() -> None:
    runner = FakeRunner((512, _fixture("smart_ok.json")))

    result = read_smart("/dev/sda", runner, 2.0)

    assert result.available is True
    assert result.execution_error_bits == (9,)
    assert result.execution_errors == ("unrecognised smartctl exit bit 9",)


def test_read_smart_negative_exit_code_is_execution_error() -> None:
    runner = FakeRunner((-9, _fixture("smart_minimal.json")))

    result = read_smart("/dev/sda", runner, 2.0)

    assert result.available is True
    assert result.exit_code == -9
    assert result.execution_errors == ("smartctl terminated with code -9",)


def test_read_smart_missing_values_stay_missing() -> None:
    runner = FakeRunner((0, _fixture("smart_minimal.json")))

    result = read_smart("/dev/sdc", runner, 2.0)

    data = result.data
    assert data is not None
    assert result.ok is True
    assert data.model_name is None
    assert data.serial_number is None
    assert data.firmware_version is None
    assert data.smart_supported is None
    assert data.smart_enabled is None
    assert data.health_passed is None
    assert data.temperature_c is None
    assert data.power_on_hours is None
    assert data.attributes == ()
    assert data.messages == ()
    assert dict(data)["device"] == {"name": "/dev/sdc", "type": "ata"}


def test_smart_data_mapping_is_read_only() -> None:
    runner = FakeRunner((0, _fixture("smart_ok.json")))

    result = read_smart("/dev/sda", runner, 2.0)

    data = result.data
    assert data is not None
    assert dict(data)["model_name"] == "Example SSD"
    with pytest.raises(TypeError):
        data.raw["model_name"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        data.attributes[0]["name"] = "changed"  # type: ignore[index]


def test_read_smart_invalid_runner_result() -> None:
    result = read_smart("/dev/sda", FakeRunner(object()), 2.0)

    assert result.available is False
    assert result.execution_errors[0].startswith("invalid runner result")


@pytest.mark.parametrize("device", ["", "-a", "--all", "bad\x00device"])
def test_read_smart_rejects_invalid_device(device: str) -> None:
    runner = FakeRunner((0, "{}"))

    with pytest.raises(ValueError):
        read_smart(device, runner, 1.0)

    assert runner.calls == []


@pytest.mark.parametrize("timeout", [0, -1.0])
def test_read_smart_rejects_non_positive_timeout(timeout: float) -> None:
    runner = FakeRunner((0, "{}"))

    with pytest.raises(ValueError, match="timeout"):
        read_smart("/dev/sda", runner, timeout)

    assert runner.calls == []
