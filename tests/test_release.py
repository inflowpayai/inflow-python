import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import textwrap
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


@pytest.mark.parametrize(
    "event,publish,repository,ref,same_commit,success",
    [
        ("pull_request", "false", "inflowpayai/inflow-python", "refs/pull/17/merge", False, True),
        (
            "workflow_dispatch",
            "false",
            "inflowpayai/inflow-python",
            "refs/heads/main",
            False,
            False,
        ),
        ("workflow_dispatch", "true", "inflowpayai/inflow-python", "refs/heads/main", False, False),
        ("workflow_dispatch", "false", "inflowpayai/inflow-python", "refs/heads/main", True, True),
        ("workflow_dispatch", "true", "inflowpayai/inflow-python", "refs/heads/main", True, True),
        ("workflow_dispatch", "true", "nkavian/inflow-python", "refs/heads/main", True, False),
        (
            "workflow_dispatch",
            "true",
            "inflowpayai/inflow-python",
            "refs/heads/feature",
            True,
            False,
        ),
    ],
)
def test_workflow_release_tag_guard(
    tmp_path: Path,
    event: str,
    publish: str,
    repository: str,
    ref: str,
    same_commit: bool,
    success: bool,
) -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/release.yml").read_text()
    step = workflow.split("      - name: Validate release\n", 1)[1].split("      - name:", 1)[0]
    script = textwrap.dedent(step.split("        run: |\n", 1)[1]).replace(
        "python scripts/release.py", shlex.quote(sys.executable) + " scripts/release.py"
    )
    (tmp_path / "scripts").mkdir()
    shutil.copy(root / "scripts/release.py", tmp_path / "scripts/release.py")
    (tmp_path / "pyproject.toml").write_text('[project]\nname="inflowpay"\nversion="0.1.0"\n')

    def git(*arguments: str) -> str:
        return subprocess.check_output(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "-c",
                "commit.gpgsign=false",
                "-c",
                f"core.hooksPath={os.devnull}",
                *arguments,
            ],
            cwd=tmp_path,
            text=True,
            stderr=subprocess.PIPE,
        ).strip()

    git("init")
    git("add", ".")
    git("commit", "-m", "Initial version")
    git("tag", "v0.1.0")
    if not same_commit:
        git("commit", "--allow-empty", "-m", "Feature")
    result = subprocess.run(
        ["bash", "-e", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PUBLISH": publish,
            "GITHUB_EVENT_NAME": event,
            "GITHUB_REPOSITORY": repository,
            "GITHUB_REF": ref,
            "GITHUB_SHA": git("rev-parse", "HEAD"),
            "GITHUB_OUTPUT": str(tmp_path / "output"),
        },
    )
    assert (result.returncode == 0) is success, result.stderr
    if success:
        assert (tmp_path / "output").read_text() == "version=0.1.0\n"
