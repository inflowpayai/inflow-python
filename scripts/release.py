"""Validate release artifacts and refuse conflicting PyPI uploads."""

import argparse
import hashlib
import json
import re
import tomllib
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen


def project_version(project: Path) -> str:
    with project.open("rb") as source:
        metadata = tomllib.load(source)["project"]
    version = metadata["version"]
    if (
        metadata["name"] != "inflowpay"
        or not isinstance(version, str)
        or not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version)
    ):
        raise ValueError("Expected inflowpay with a stable semantic version")
    return version


def check_registry(directory: Path, version: str, *, complete: bool = False) -> None:
    expected = {f"inflowpay-{version}.tar.gz", f"inflowpay-{version}-py3-none-any.whl"}
    files = {path.name: path for path in directory.iterdir()}
    if set(files) != expected or not all(path.is_file() for path in files.values()):
        raise ValueError("Expected exactly the release wheel and source distribution")
    try:
        with urlopen(f"https://pypi.org/pypi/inflowpay/{version}/json", timeout=30) as response:
            published = json.load(response)["urls"]
    except HTTPError as error:
        if error.code != 404:
            raise
        published = []
    found = set()
    for entry in published:
        name = entry["filename"]
        if (
            name not in files
            or entry["digests"]["sha256"] != hashlib.sha256(files[name].read_bytes()).hexdigest()
        ):
            raise ValueError("PyPI contains different artifacts for this version")
        found.add(name)
    if complete and found != expected:
        raise ValueError("PyPI does not yet contain both verified artifacts")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist-dir", type=Path)
    parser.add_argument("--complete", action="store_true")
    arguments = parser.parse_args()
    version = project_version(Path(__file__).resolve().parents[1] / "pyproject.toml")
    if arguments.dist_dir:
        check_registry(arguments.dist_dir, version, complete=arguments.complete)
    elif arguments.complete:
        parser.error("--complete requires --dist-dir")
    print(version)


if __name__ == "__main__":
    main()
