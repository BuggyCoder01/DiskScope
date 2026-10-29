"""Read-only SMART data access through smartctl JSON.

Only ``smartctl -j -a DEVICE`` is issued. The injected runner must execute the
argument list without a shell; tests use fakes and never run smartctl. This
module deliberately does not interpret health; it exposes parsed data plus the
execution/device split of smartctl's exit-code bitmask.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .models import CommandResult, Runner, SmartData

_EXECUTION_BITS = {
    0: "command line did not parse",
    1: "device open failed or device did not identify",
    2: "SMART command failed or checksum error",
}
_DEVICE_BITS = {
    3: "SMART status check returned DISK FAILING",
    4: "pre-fail attribute at or below threshold",
    5: "attribute was at or below threshold in the past",
    6: "error log contains errors",
    7: "self-test log contains errors",
}
_DRIVE_ERRORS = {
    21,  # device not ready
    55,  # network resource unavailable
    1167,  # device not connected
    1231,  # network location unreachable
}


@dataclass(frozen=True, slots=True)
class SmartResult:
    """Parsed smartctl data plus exit-code classification."""

    device: str
    data: SmartData | None
    execution_errors: tuple[str, ...] = ()
    device_conditions: tuple[str, ...] = ()
    exit_code: int | None = None
    execution_error_bits: tuple[int, ...] = ()
    device_condition_bits: tuple[int, ...] = ()
    stderr: str = ""

    @property
    def available(self) -> bool:
        """Whether parseable JSON was returned, even if bits report problems."""
        return self.data is not None

    @property
    def ok(self) -> bool:
        """Whether data was returned with no execution or device conditions."""
        return self.data is not None and not self.execution_errors and not self.device_conditions

    @property
    def error(self) -> str | None:
        return self.execution_errors[0] if self.execution_errors else None

    @property
    def reasons(self) -> tuple[str, ...]:
        return self.execution_errors + self.device_conditions

    @property
    def smart(self) -> SmartData | None:
        return self.data

    @property
    def smart_data(self) -> SmartData | None:
        return self.data

    @property
    def payload(self) -> Mapping[str, Any] | None:
        return self.data.raw if self.data is not None else None

    @property
    def messages(self) -> tuple[str, ...]:
        return self.data.messages if self.data is not None else ()


def read_smart(
    device: str | os.PathLike[str],
    runner: Runner,
    timeout: float,
) -> SmartResult:
    """Read SMART data for ``device`` using ``smartctl -j -a``.

    ``timeout`` is passed to the injected runner. Failures are returned as a
    result with no data and a reason; they do not raise except for invalid
    arguments. A nonzero exit code never discards usable JSON.
    """
    device_text = os.fspath(device)
    if not device_text or "\x00" in device_text:
        raise ValueError("device must be a non-empty path")
    if device_text.startswith("-"):
        raise ValueError("device must not start with '-'")
    if timeout <= 0:
        raise ValueError("timeout must be positive")

    argv = ["smartctl", "-j", "-a", device_text]
    try:
        raw = runner(argv, timeout)
    except subprocess.TimeoutExpired as exc:
        return _failure(device_text, f"smartctl timed out after {timeout:g}s", exc)
    except FileNotFoundError as exc:
        return _failure(device_text, "smartctl not found", exc)
    except PermissionError as exc:
        return _failure(device_text, "permission denied", exc)
    except subprocess.CalledProcessError as exc:
        try:
            result = CommandResult.coerce(exc)
        except TypeError as coercion_error:
            return _failure(device_text, f"invalid runner result: {coercion_error}", exc)
        return _parse_result(device_text, result)
    except OSError as exc:
        return _failure(device_text, _describe_os_error(exc), exc)
    except Exception as exc:  # noqa: BLE001 - relay unexpected runner failures
        return _failure(device_text, f"smartctl failed: {exc}", exc)

    try:
        result = CommandResult.coerce(raw)
    except TypeError as exc:
        return _failure(device_text, f"invalid runner result: {exc}")
    return _parse_result(device_text, result)


def _parse_result(device: str, result: CommandResult) -> SmartResult:
    execution_bits, execution_errors, device_bits, device_conditions = _split_exit_code(
        result.returncode
    )
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    if not stdout:
        return SmartResult(
            device=device,
            data=None,
            execution_errors=(*execution_errors, "smartctl returned no JSON output"),
            device_conditions=device_conditions,
            exit_code=result.returncode,
            execution_error_bits=execution_bits,
            device_condition_bits=device_bits,
            stderr=stderr,
        )

    try:
        payload: Any = json.loads(stdout)
    except json.JSONDecodeError as exc:
        return SmartResult(
            device=device,
            data=None,
            execution_errors=(*execution_errors, f"invalid JSON: {exc}"),
            device_conditions=device_conditions,
            exit_code=result.returncode,
            execution_error_bits=execution_bits,
            device_condition_bits=device_bits,
            stderr=stderr,
        )

    if not isinstance(payload, Mapping):
        return SmartResult(
            device=device,
            data=None,
            execution_errors=(*execution_errors, "JSON root is not an object"),
            device_conditions=device_conditions,
            exit_code=result.returncode,
            execution_error_bits=execution_bits,
            device_condition_bits=device_bits,
            stderr=stderr,
        )

    return SmartResult(
        device=device,
        data=_build_smart_data(device, payload),
        execution_errors=execution_errors,
        device_conditions=device_conditions,
        exit_code=result.returncode,
        execution_error_bits=execution_bits,
        device_condition_bits=device_bits,
        stderr=stderr,
    )


def _split_exit_code(
    code: int,
) -> tuple[tuple[int, ...], tuple[str, ...], tuple[int, ...], tuple[str, ...]]:
    execution_bits: list[int] = []
    execution_errors: list[str] = []
    device_bits: list[int] = []
    device_conditions: list[str] = []

    if code < 0:
        return (), (f"smartctl terminated with code {code}",), (), ()

    for bit, message in _EXECUTION_BITS.items():
        if code & (1 << bit):
            execution_bits.append(bit)
            execution_errors.append(message)
    for bit, message in _DEVICE_BITS.items():
        if code & (1 << bit):
            device_bits.append(bit)
            device_conditions.append(message)

    for bit in range(8, code.bit_length()):
        if code & (1 << bit):
            execution_bits.append(bit)
            execution_errors.append(f"unrecognised smartctl exit bit {bit}")

    return (
        tuple(execution_bits),
        tuple(execution_errors),
        tuple(device_bits),
        tuple(device_conditions),
    )


def _build_smart_data(device: str, payload: Mapping[str, Any]) -> SmartData:
    smart_support = _mapping(payload.get("smart_support"))
    smart_status = _mapping(payload.get("smart_status"))
    power_on_time = _mapping(payload.get("power_on_time"))
    nvme_log = _mapping(payload.get("nvme_smart_health_information_log"))
    attributes = _attributes(payload.get("ata_smart_attributes"))

    return SmartData(
        device=device,
        raw=MappingProxyType(dict(payload)),
        model_name=_text(payload.get("model_name")),
        serial_number=_text(payload.get("serial_number")),
        firmware_version=_text(payload.get("firmware_version")),
        smart_supported=_boolean(smart_support.get("available")),
        smart_enabled=_boolean(smart_support.get("enabled")),
        health_passed=_boolean(smart_status.get("passed")),
        temperature_c=_temperature(payload, nvme_log),
        power_on_hours=_power_on_hours(payload, power_on_time, nvme_log),
        attributes=attributes,
    )


def _temperature(
    payload: Mapping[str, Any],
    nvme_log: Mapping[str, Any],
) -> int | None:
    temperature = payload.get("temperature")
    if isinstance(temperature, Mapping):
        value = _integer(temperature.get("current"))
    else:
        value = _integer(temperature)
    if value is not None:
        return value

    scsi_temperature = payload.get("scsi_temperature")
    if isinstance(scsi_temperature, Mapping):
        value = _integer(scsi_temperature.get("current"))
        if value is not None:
            return value

    return _integer(nvme_log.get("temperature"))


def _power_on_hours(
    payload: Mapping[str, Any],
    power_on_time: Mapping[str, Any],
    nvme_log: Mapping[str, Any],
) -> int | None:
    value = _integer(power_on_time.get("hours"))
    if value is not None:
        return value
    value = _integer(payload.get("power_on_hours"))
    if value is not None:
        return value
    return _integer(nvme_log.get("power_on_hours"))


def _attributes(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Mapping):
        return ()
    table = value.get("table")
    if not isinstance(table, list):
        return ()
    result: list[Mapping[str, Any]] = []
    for item in table:
        if isinstance(item, Mapping):
            result.append(MappingProxyType(dict(item)))
    return tuple(result)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return str(value).strip() or None


def _integer(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip(), 10)
        except ValueError:
            return None
    return None


def _boolean(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _describe_os_error(exc: OSError) -> str:
    winerror = getattr(exc, "winerror", None)
    if winerror in _DRIVE_ERRORS:
        return f"device unavailable: {exc.strerror or exc}"
    if isinstance(exc, ConnectionError):
        return f"device disconnected: {exc}"
    return f"smartctl failed: {exc}"


def _failure(
    device: str,
    message: str,
    error: BaseException | None = None,
) -> SmartResult:
    stderr = ""
    if error is not None:
        raw_stderr = getattr(error, "stderr", None)
        if raw_stderr is not None:
            stderr = (
                raw_stderr.decode("utf-8", errors="replace")
                if isinstance(raw_stderr, bytes)
                else str(raw_stderr)
            ).strip()
    return SmartResult(
        device=device,
        data=None,
        execution_errors=(message,),
        exit_code=None,
        stderr=stderr,
    )
