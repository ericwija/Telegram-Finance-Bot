"""NFR-MNT-01 / AC-P1-01: the import-linter layering contracts hold and actually bite."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import finbot

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_PACKAGE = Path(finbot.__file__).resolve().parent
LINT_IMPORTS = Path(sys.executable).with_name("lint-imports")


def _lint(
    config: Path, cwd: Path, extra_path: Path | None = None
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if extra_path is not None:
        env["PYTHONPATH"] = str(extra_path)
    return subprocess.run(
        [str(LINT_IMPORTS), "--config", str(config), "--no-cache"],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=env,
        timeout=120,
        check=False,
    )


@pytest.fixture(scope="module")
def lint_imports_available() -> None:
    if not LINT_IMPORTS.exists():
        pytest.fail(f"lint-imports not installed next to {sys.executable}; run `uv sync`")


@pytest.mark.usefixtures("lint_imports_available")
def test_contracts_pass_on_current_tree() -> None:
    result = _lint(REPO_ROOT / "pyproject.toml", REPO_ROOT)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "BROKEN" not in result.stdout


@pytest.mark.usefixtures("lint_imports_available")
@pytest.mark.parametrize(
    ("module", "statement", "contract"),
    [
        ("domain/bad.py", "import finbot.adapters.telegram", "Layered architecture"),
        ("domain/bad.py", "import finbot.config", "Domain is pure"),
        ("services/bad.py", "import finbot.infra.db", "Services and authz"),
        ("adapters/cli/bad.py", "import finbot.adapters.telegram", "independent"),
    ],
)
def test_deliberate_violation_is_detected(
    tmp_path: Path, module: str, statement: str, contract: str
) -> None:
    copy_root = tmp_path / "src"
    shutil.copytree(SRC_PACKAGE, copy_root / "finbot")
    (copy_root / "finbot" / module).write_text(f"{statement}  # deliberate violation\n")
    shutil.copy(REPO_ROOT / "pyproject.toml", tmp_path / "pyproject.toml")

    result = _lint(tmp_path / "pyproject.toml", tmp_path, extra_path=copy_root)
    assert result.returncode != 0, result.stdout
    assert contract in result.stdout
    assert "BROKEN" in result.stdout
