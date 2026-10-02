import hashlib
import io
import json
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError

import pytest
from scripts.release import check_registry, project_version


@pytest.mark.parametrize("version", ["0.1.0", "1.0.0", "20.10.3"])
def test_release_version(tmp_path: Path, version: str) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(f'[project]\nname="inflowpay"\nversion="{version}"\n')
    assert project_version(project) == version


@pytest.mark.parametrize("version", ["01.0.0", "v1.0.0", "1.0.0rc1", "1.0", "1.0.0+build"])
def test_invalid_release_version(tmp_path: Path, version: str) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(f'[project]\nname="inflowpay"\nversion="{version}"\n')
    with pytest.raises(ValueError):
        project_version(project)


@pytest.mark.parametrize(
    "state", ["absent", "partial", "complete", "conflict", "foreign", "unavailable"]
)
def test_registry_artifact_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    names = ["inflowpay-0.1.0.tar.gz", "inflowpay-0.1.0-py3-none-any.whl"]
    for name in names:
        (tmp_path / name).write_bytes(b"verified artifact")

    def response(url: str, *, timeout: int) -> io.BytesIO:
        assert url == "https://pypi.org/pypi/inflowpay/0.1.0/json" and timeout == 30
        if state in ("absent", "unavailable"):
            raise HTTPError(url, 404 if state == "absent" else 503, "test", Message(), None)
        entries = [
            {
                "filename": name,
                "digests": {
                    "sha256": hashlib.sha256(
                        b"different" if state == "conflict" else b"verified artifact"
                    ).hexdigest()
                },
            }
            for name in (names[:1] if state == "partial" else names)
        ]
        if state == "foreign":
            entries[0]["filename"] = "other.whl"
        return io.BytesIO(json.dumps({"urls": entries}).encode())

    monkeypatch.setattr("scripts.release.urlopen", response)
    if state in ("conflict", "foreign", "unavailable"):
        with pytest.raises((ValueError, HTTPError)):
            check_registry(tmp_path, "0.1.0")
    else:
        check_registry(tmp_path, "0.1.0")
        if state == "complete":
            check_registry(tmp_path, "0.1.0", complete=True)
        else:
            with pytest.raises(ValueError):
                check_registry(tmp_path, "0.1.0", complete=True)


def test_missing_artifacts(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        check_registry(tmp_path, "0.1.0")


def test_wrong_project(tmp_path: Path) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text('[project]\nname="another-project"\nversion="0.1.0"\n')
    with pytest.raises(ValueError):
        project_version(project)
