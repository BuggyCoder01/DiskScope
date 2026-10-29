"""Read-only physical drive discovery through PowerShell JSON.

The subprocess boundary is injected: ``runner(argv, timeout)`` must execute the
argument list without a shell and return returncode/stdout/stderr data. Tests
use a fake runner and never start PowerShell.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from typing import Any

from .models import CommandResult, DriveInfo, Runner

_QUERY_TIMEOUT = 30.0
_POWERSHELL = "powershell.exe"
_SELECT_FIELDS = (
    "DeviceId",
    "FriendlyName",
    "MediaType",
    "BusType",
    "Size",
    "HealthStatus",
    "OperationalStatus",
    "SerialNumber",
    "Model",
    "FirmwareVersion",
)


class DriveQueryError(RuntimeError):
    """PowerShell or JSON parsing failed while listing drives."""


def _powershell_command() -> list[str]:
    fields = ",".join(_SELECT_FIELDS)
    script = f"@(Get-PhysicalDisk | Select-Object {fields}) | ConvertTo-Json -Depth 3 -Compress"
    return [
        _POWERSHELL,
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        script,
    ]


def list_drives(runner: Runner) -> list[DriveInfo]:
    """List physical drives reported by ``Get-PhysicalDisk``.

    No administrator rights are required. The runner is responsible for
    executing the supplied argument list without a shell.
    """
    argv = _powershell_command()
    try:
        raw = runner(argv, _QUERY_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise DriveQueryError(f"Get-PhysicalDisk timed out after {_QUERY_TIMEOUT:g}s") from exc
    except FileNotFoundError as exc:
        raise DriveQueryError("PowerShell not found (powershell.exe)") from exc
    except PermissionError as exc:
        raise DriveQueryError("permission denied while running Get-PhysicalDisk") from exc
    except OSError as exc:
        raise DriveQueryError(f"Get-PhysicalDisk failed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - surface the runner failure
        raise DriveQueryError(f"Get-PhysicalDisk failed: {exc}") from exc

    try:
        result = CommandResult.coerce(raw)
    except TypeError as exc:
        raise DriveQueryError(f"invalid runner result: {exc}") from exc

    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        suffix = f": {detail[:300]}" if detail else ""
        raise DriveQueryError(f"Get-PhysicalDisk exited with code {result.returncode}{suffix}")

    return parse_drives_json(result.stdout)


def parse_drives_json(text: str) -> list[DriveInfo]:
    """Parse ``Get-PhysicalDisk | ConvertTo-Json`` output.

    ``ConvertTo-Json`` emits an object for one disk and an array for several, so
    both shapes are accepted. Empty output means no physical disks.
    """
    if not text.strip():
        return []
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DriveQueryError(f"invalid JSON from Get-PhysicalDisk: {exc}") from exc

    if payload is None:
        return []
    if isinstance(payload, Mapping):
        items: list[Any] = [payload]
    elif isinstance(payload, list):
        items = payload
    else:
        raise DriveQueryError("Get-PhysicalDisk JSON must be an object or an array")

    drives: list[DriveInfo] = []
    for index, item in enumerate(items):
        if item is None:
            continue
        if not isinstance(item, Mapping):
            raise DriveQueryError(f"Get-PhysicalDisk item {index} is not an object")
        drives.append(_drive_from_mapping(item))
    return drives


def _drive_from_mapping(item: Mapping[str, Any]) -> DriveInfo:
    return DriveInfo(
        device_id=_text(item.get("DeviceId")) or "",
        friendly_name=_text(item.get("FriendlyName")) or "",
        media_type=_text(item.get("MediaType")),
        bus_type=_text(item.get("BusType")),
        size_bytes=_integer(item.get("Size")),
        health_status=_text(item.get("HealthStatus")),
        operational_status=_statuses(item.get("OperationalStatus")),
        serial_number=_text(item.get("SerialNumber")),
        model=_text(item.get("Model")),
        firmware_version=_text(item.get("FirmwareVersion")),
    )


def _text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, (int, float, bool)):
        return str(value)
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


def _statuses(value: object) -> tuple[str, ...]:
    if value is None or isinstance(value, Mapping):
        return ()
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, (list, tuple)):
        result: list[str] = []
        for item in value:
            text = _text(item)
            if text is not None:
                result.append(text)
        return tuple(result)
    text = _text(value)
    return (text,) if text is not None else ()
