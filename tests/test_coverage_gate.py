import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_coverage.py"


@pytest.mark.parametrize(
    ("lines", "branches", "branch_coverage", "empty", "success"),
    [
        (0, 0, True, False, True),
        (1, 0, True, False, False),
        (0, 1, True, False, False),
        (0, 0, False, False, False),
        (0, 0, True, True, False),
    ],
)
def test_coverage_gate(
    tmp_path: Path, lines: int, branches: int, branch_coverage: bool, empty: bool, success: bool
) -> None:
    report = {
        "meta": {"branch_coverage": branch_coverage},
        "files": {}
        if empty
        else {
            "src/inflowpay/__init__.py": {
                "summary": {"missing_lines": lines, "missing_branches": branches}
            }
        },
    }
    (tmp_path / "coverage.json").write_text(json.dumps(report))
    result = subprocess.run(
        [sys.executable, str(SCRIPT)], cwd=tmp_path, capture_output=True, text=True
    )
    assert (result.returncode == 0) is success
    if not success:
        assert "ValueError" in result.stderr
