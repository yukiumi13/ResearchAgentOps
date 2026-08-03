from __future__ import annotations

from typer.testing import CliRunner

from researchctl.trusted_linear_host import app


def test_trusted_linear_host_exposes_explicit_shadow_subcommand() -> None:
    runner = CliRunner()

    root_help = runner.invoke(app, ["--help"])
    assert root_help.exit_code == 0
    assert "shadow" in root_help.output

    shadow_help = runner.invoke(app, ["shadow", "--help"])
    assert shadow_help.exit_code == 0
    assert "--dispatch-artifact" in shadow_help.output
    assert "--output" in shadow_help.output
    assert "Validate one accepted merge" in shadow_help.output
