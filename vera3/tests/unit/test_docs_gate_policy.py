"""Exercise the actual GitHub Actions documentation gates in a temporary repo."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[3] / ".github/workflows/deploy.yml"


def _bash() -> str:
    found = shutil.which("bash")
    if found:
        return found
    windows_git = Path("C:/Program Files/Git/bin/bash.exe")
    if windows_git.exists():
        return str(windows_git)
    pytest.skip("bash is unavailable")


def _git(repo: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True, encoding="utf-8"
    ).strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "docs-gate@example.invalid")
    _git(root, "config", "user.name", "Docs Gate Test")
    script = root / "vera3/scripts/ci_diff_base.sh"
    script.parent.mkdir(parents=True)
    script.write_text("git rev-parse HEAD^\n", encoding="utf-8")
    code = root / "vera3/shared/example.py"
    code.parent.mkdir(parents=True)
    code.write_text("VALUE = 1\n", encoding="utf-8")
    docs = root / "vera3/docs/example.md"
    docs.parent.mkdir(parents=True)
    docs.write_text("# Example\n", encoding="utf-8")
    _commit(root, "chore: baseline")
    return root


def _run_gate(repo: Path, name: str, head: str) -> subprocess.CompletedProcess[str]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    if name == "docs":
        script = workflow["jobs"]["docs"]["steps"][1]["run"]
    else:
        steps = workflow["jobs"]["quality"]["steps"]
        script = next(
            step["run"] for step in steps if step.get("name") == "Docs name-sync"
        )
        script = script.replace(
            "${{ steps.base.outputs.base }}", "$(git rev-parse HEAD^)"
        )
        script = script.replace("${{ steps.base.outputs.head }}", "$CI_HEAD")
    return subprocess.run(
        [_bash(), "-c", script],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, "CI_HEAD": head},
        check=False,
    )


def test_code_change_requires_docs_even_with_old_bypass_marker(repo: Path) -> None:
    (repo / "vera3/shared/example.py").write_text("VALUE = 2\n", encoding="utf-8")
    head = _commit(repo, "feat: changed code\n\ndocs-not-needed")
    result = _run_gate(repo, "docs", head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "vera3/docs was not updated" in result.stdout


def test_public_symbol_requires_matching_docs(repo: Path) -> None:
    (repo / "vera3/shared/example.py").write_text(
        "def new_contract():\n    return 2\n", encoding="utf-8"
    )
    (repo / "vera3/docs/example.md").write_text("# Updated example\n", encoding="utf-8")
    head = _commit(repo, "feat: new function\n\ndocs-not-needed")
    result = _run_gate(repo, "quality", head)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "new_contract" in result.stdout


def test_updated_contract_doc_passes_both_gates(repo: Path) -> None:
    (repo / "vera3/shared/example.py").write_text(
        "def new_contract():\n    return 2\n", encoding="utf-8"
    )
    (repo / "vera3/docs/example.md").write_text("# new_contract\n", encoding="utf-8")
    head = _commit(repo, "feat: documented function")
    assert _run_gate(repo, "docs", head).returncode == 0
    assert _run_gate(repo, "quality", head).returncode == 0
