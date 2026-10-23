"""Thin Typer commands; configuration and application imports remain lazy."""

from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

import typer
from pydantic import ValidationError
from pydantic_settings import SettingsError

from pokerdemo.settings import Settings, load_settings

app = typer.Typer(
    help="Local PokerEngine demo and private recordings.",
    invoke_without_command=True,
    pretty_exceptions_enable=False,
)
config_app = typer.Typer(help="Read and validate explicit local configuration.")
app.add_typer(config_app, name="config")


def show_version(value: bool) -> None:
    if value:
        typer.echo(version("pokerdemo"))
        raise typer.Exit()


@app.callback()
def main(
    version_flag: Annotated[
        bool, typer.Option("--version", callback=show_version, is_eager=True)
    ] = False,
) -> None:
    """Import no settings, tools or game code merely to show help/version."""


def configured(config: Path | None, **overrides: Any) -> Settings:
    try:
        return load_settings(config, **overrides)
    except ValidationError, SettingsError, OSError, UnicodeError:
        typer.echo("Invalid or unavailable configuration.", err=True)
        raise typer.Exit(2) from None


@config_app.command("show")
def show_config(config: Path | None = None, workspace: Path | None = None) -> None:
    """Inspect settings without opening sockets, launching tools or writing files."""
    settings = configured(config, workspace=workspace)
    typer.echo(settings.model_dump_json(indent=2))


@app.command()
def launch(
    config: Path | None = None,
    workspace: Path | None = None,
    tmux: str | None = None,
    detach: bool = False,
) -> None:
    """Open the real server above two human-controlled clients."""
    from pokerdemo.session import launch as launch_session

    launch_session(configured(config, workspace=workspace, tmux=tmux), detach=detach)


@app.command()
def attach(
    config: Path | None = None, workspace: Path | None = None, tmux: str | None = None
) -> None:
    """Attach only to this workspace's demo session."""
    from pokerdemo.session import DemoSession

    DemoSession(configured(config, workspace=workspace, tmux=tmux)).attach()


@app.command()
def stop(
    config: Path | None = None, workspace: Path | None = None, tmux: str | None = None
) -> None:
    """Stop only this workspace's demo session and its three application processes."""
    from pokerdemo.session import DemoSession

    DemoSession(configured(config, workspace=workspace, tmux=tmux)).stop()


@app.command()
def doctor(config: Path | None = None, workspace: Path | None = None) -> None:
    """Check installed prerequisites and the exact engine wheel without starting a game."""
    from pokerdemo.session import verify_wheel
    from pokerdemo.tools import resolve_tool, resolve_wheel

    settings = configured(config, workspace=workspace)
    artifact = verify_wheel(resolve_wheel(settings))
    for name in ("tmux", "agg"):
        tool = resolve_tool(name, getattr(settings, name)).describe()
        typer.echo(f"{name}: {tool['version']} ({tool['origin']})")
    typer.echo(f"engine: {artifact['wheelVersion']} SHA256 {artifact['wheelSha256']}")


@app.command()
def record(
    config: Path | None = None,
    workspace: Path | None = None,
    output_root: Path | None = None,
    engine_wheel: Path | None = None,
    tmux: str | None = None,
    agg: str | None = None,
    delay: float | None = None,
) -> None:
    """One command: actual PTY capture, official agg replay, unchanged PNG frames."""
    from pokerdemo.render import render
    from pokerdemo.session import record as record_session
    from pokerdemo.tools import resolve_tool

    settings = configured(
        config,
        workspace=workspace,
        output_root=output_root,
        engine_wheel=engine_wheel,
        tmux=tmux,
        agg=agg,
        delay=delay,
    )
    renderer = resolve_tool("agg", settings.agg)
    # Fail before starting a game if the separately installed image dependency is missing.
    from PIL import Image

    if not Image.__version__:
        raise RuntimeError("Pillow is unavailable")
    destination = settings.output_root.expanduser()
    if not destination.is_absolute():
        destination = settings.workspace.expanduser().absolute() / destination
    destination = destination / (
        datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    )
    typer.echo(f"Local recording: {destination}")
    record_session(settings, destination)
    frames = render(destination, renderer, settings.font_family)
    typer.echo(f"GIF: {destination / 'demo.gif'}")
    for frame in frames:
        typer.echo(f"Frame: {frame}")
    typer.echo(f"Receipt: {destination / 'recording.json'}")
