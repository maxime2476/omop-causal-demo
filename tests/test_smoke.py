from typer.testing import CliRunner

from omop_causal import __version__
from omop_causal.cli import app


def test_version_is_defined() -> None:
    assert __version__ == "0.1.0"


def test_cli_version_command() -> None:
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout
