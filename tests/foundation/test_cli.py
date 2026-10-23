"""Exercise CLI parsing, lazy settings, source priority and safe errors."""

import json
import os
import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsError
from typer.testing import CliRunner

from pokerdemo import cli
from pokerdemo.cli import app
from pokerdemo.settings import Settings, load_settings


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("POKERDEMO_"):
            monkeypatch.delenv(name)


@pytest.mark.parametrize(
    "arguments",
    [[], ["--help"], ["config", "--help"], ["config", "show", "--help"], ["--version"]],
)
def test_help_and_version_ignore_invalid_configuration(arguments: list[str]) -> None:
    environment = {"POKERDEMO_DELAY": "invalid"}
    result = CliRunner().invoke(app, arguments, env=environment)
    assert result.exit_code == 0, result.output
    assert not result.stderr
    if not arguments:
        assert result.stdout == ""
    if arguments == ["--version"]:
        assert result.stdout.strip() == version("pokerdemo")


@pytest.mark.parametrize(
    "arguments",
    [
        ["missing"],
        ["config", "missing"],
        ["config", "show", "extra"],
        ["config", "show", "--limit"],
        ["config", "show", "--limit", "invalid"],
    ],
)
def test_invalid_arguments_are_errors(arguments: list[str]) -> None:
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code == 2
    assert result.stderr and not result.stdout


def test_cli_env_file_defaults_priority_and_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "параметры проверки.env"
    config.write_text("POKERDEMO_WORKSPACE=/from-file\nPOKERDEMO_DELAY=3\n")
    before = config.read_bytes()
    runner = CliRunner()
    assert json.loads(runner.invoke(app, ["config", "show"]).stdout)["delay"] == 1.5
    arguments = ["config", "show", "--config", str(config)]
    assert json.loads(runner.invoke(app, arguments).stdout)["workspace"] == "/from-file"
    monkeypatch.setenv("POKERDEMO_WORKSPACE", "/from-env")
    assert json.loads(runner.invoke(app, arguments).stdout)["workspace"] == "/from-env"
    result = runner.invoke(app, [*arguments, "--workspace", "/from-cli"], input="")
    assert result.exit_code == 0
    assert json.loads(result.stdout)["workspace"] == "/from-cli"
    assert config.read_bytes() == before
    assert list(tmp_path.iterdir()) == [config]


def test_invalid_settings_and_unknown_fields_do_not_leak(tmp_path: Path) -> None:
    secret = "SYNTHETIC_SECRET_DO_NOT_LOG"
    config = tmp_path / "secret.env"
    config.write_text(f"POKERDEMO_DELAY={secret}\n")
    result = CliRunner().invoke(app, ["config", "show", "--config", str(config)])
    assert result.exit_code == 2
    assert result.stderr == "Invalid or unavailable configuration.\n"
    assert secret not in result.output and not result.stdout
    assert config.read_text().endswith(secret + "\n")
    with pytest.raises(ValidationError) as failure:
        Settings(delay=secret)
    assert secret not in str(failure.value)
    config.write_text(f"UNEXPECTED={secret}\n")
    assert CliRunner().invoke(app, ["config", "show", "--config", str(config)]).exit_code == 2
    assert (
        CliRunner().invoke(app, ["config", "show", "--config", str(tmp_path / "missing")]).exit_code
        == 2
    )


def test_settings_serialization_schema_and_explicit_null() -> None:
    settings = load_settings(None, delay=0.4, workspace=Path("/demo"))
    assert settings.delay == 0.4 and settings.workspace == Path("/demo")
    assert Settings.model_validate_json(settings.model_dump_json()) == settings
    assert Settings.model_json_schema()["properties"]["delay"]["minimum"] == 0.4
    with pytest.raises(ValidationError):
        Settings.model_validate({"delay": None})


def test_real_executable_and_module_outside_checkout(tmp_path: Path) -> None:
    executable = shutil.which("pokerDemo")
    assert executable is not None
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment["POKERDEMO_DELAY"] = "invalid"
    for command in [
        [executable, "--version"],
        [sys.executable, "-m", "pokerdemo", "--version"],
    ]:
        result = subprocess.run(
            command,
            cwd=tmp_path,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == version("pokerdemo")
        assert not result.stderr


@pytest.mark.parametrize("failure", [SettingsError("SYNTHETIC_PRIVATE_INPUT"), KeyboardInterrupt()])
def test_source_error_and_cancellation_are_safe(
    failure: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(config: Path | None, **overrides: object) -> Settings:
        raise failure

    monkeypatch.setattr(cli, "load_settings", fail)
    result = CliRunner().invoke(app, ["config", "show"], input="")
    assert result.exit_code == (2 if isinstance(failure, SettingsError) else 130)
    assert not result.stdout
    assert "SYNTHETIC_PRIVATE_INPUT" not in result.output
    if isinstance(failure, SettingsError):
        assert result.stderr == "Invalid or unavailable configuration.\n"
    else:
        assert not result.stderr
