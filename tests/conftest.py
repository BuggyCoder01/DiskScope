from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_tree(tmp_path: Path) -> Path:
    spec = json.loads((FIXTURES / "sample_tree.json").read_text(encoding="utf-8"))
    for directory in spec["dirs"]:
        (tmp_path / directory).mkdir(parents=True, exist_ok=True)
    for name, size in spec["files"].items():
        destination = tmp_path / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"x" * size)
    return tmp_path
