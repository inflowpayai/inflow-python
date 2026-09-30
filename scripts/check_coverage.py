"""Require complete line and branch coverage for each reported source file."""

import json
from pathlib import Path


def check_coverage(path: Path) -> None:
    report = json.loads(path.read_text())
    if not report["meta"]["branch_coverage"]:
        raise ValueError("Branch coverage must be enabled")
    files = report["files"]
    if not files:
        raise ValueError("Coverage report contains no source files")
    failures = []
    for name, data in files.items():
        summary = data["summary"]
        if summary["missing_lines"] or summary["missing_branches"]:
            failures.append(name)
    if failures:
        raise ValueError("Incomplete line or branch coverage: " + ", ".join(sorted(failures)))


if __name__ == "__main__":
    check_coverage(Path("coverage.json"))
