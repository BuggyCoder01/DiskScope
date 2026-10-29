from __future__ import annotations

import subprocess
import sys


def test_core_import_does_not_pull_in_qt() -> None:
    code = (
        "import sys\n"
        "import diskscope.cli, diskscope.export, diskscope.space\n"
        "print(any(name.startswith('PySide6') for name in sys.modules))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "False"
