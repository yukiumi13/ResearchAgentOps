from __future__ import annotations

from click import Group, Option
from typer.main import get_command
from typer.testing import CliRunner

from researchctl.trusted_linear_host import app


def test_trusted_linear_host_exposes_explicit_shadow_subcommand() -> None:
    runner = CliRunner()

    root_help = runner.invoke(app, ["--help"], color=False, terminal_width=160)
    assert root_help.exit_code == 0
    assert "shadow" in root_help.output

    shadow_help = runner.invoke(
        app,
        ["shadow", "--help"],
        color=False,
        terminal_width=160,
    )
    assert shadow_help.exit_code == 0
    assert "Validate one accepted merge" in shadow_help.output

    root_command = get_command(app)
    assert isinstance(root_command, Group)
    shadow_command = root_command.commands["shadow"]
    options = {
        option
        for parameter in shadow_command.params
        if isinstance(parameter, Option)
        for option in parameter.opts
    }
    assert "--dispatch-artifact" in options
    assert "--output" in options
