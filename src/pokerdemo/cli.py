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
    from pokerdemo.runtime.session import launch as launch_session

    launch_session(configured(config, workspace=workspace, tmux=tmux), detach=detach)


@app.command()
def attach(
    config: Path | None = None, workspace: Path | None = None, tmux: str | None = None
) -> None:
    """Attach only to this workspace's demo session."""
    from pokerdemo.runtime.session import DemoSession

    DemoSession(configured(config, workspace=workspace, tmux=tmux)).attach()


@app.command()
def stop(
    config: Path | None = None, workspace: Path | None = None, tmux: str | None = None
) -> None:
    """Stop only this workspace's demo session and its three application processes."""
    from pokerdemo.runtime.session import DemoSession

    DemoSession(configured(config, workspace=workspace, tmux=tmux)).stop()


@app.command()
def doctor(config: Path | None = None, workspace: Path | None = None) -> None:
    """Check installed prerequisites and the exact engine wheel without starting a game."""
    from pokerdemo.runtime.recording import verify_wheel
    from pokerdemo.runtime.tools import resolve_tool, resolve_wheel

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
    show_cards: Annotated[
        bool | None,
        typer.Option(
            "--show-cards/--hide-cards",
            help="Show each synthetic player's own cards in the local recording (default: show).",
        ),
    ] = None,
    open_result: Annotated[bool, typer.Option("--open")] = False,
) -> None:
    """One command: actual PTY capture, official agg replay, unchanged PNG frames."""
    from pokerdemo.runtime.recording import record as record_session
    from pokerdemo.runtime.render import render
    from pokerdemo.runtime.tools import resolve_tool

    settings = configured(
        config,
        workspace=workspace,
        output_root=output_root,
        engine_wheel=engine_wheel,
        tmux=tmux,
        agg=agg,
        delay=delay,
        record_show_cards=show_cards,
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
    typer.echo(
        "Cards: each client shows its own synthetic hand; the server stays public."
        if settings.record_show_cards
        else "Cards: hidden in both clients; all recorded views are public."
    )
    typer.echo("[1/3] Playing an unattended game (~60 seconds); the game is not opened on screen.")
    record_session(settings, destination)
    frames = render(destination, renderer, settings.font_family)
    typer.echo(f"GIF: {destination / 'demo.gif'}")
    for frame in frames:
        typer.echo(f"Frame: {frame}")
    typer.echo(f"Receipt: {destination / 'recording.json'}")
    typer.echo("Done. Open the GIF with your file viewer, or use make demo-record-open next time.")
    if open_result:
        typer.launch(str(destination / "demo.gif"))


def loopback_host(host: str) -> str:
    from ipaddress import IPv4Address

    try:
        address = IPv4Address(host)
    except ValueError:
        raise typer.BadParameter("Use an IPv4 loopback address, for example 127.0.0.1") from None
    if not address.is_loopback:
        raise typer.BadParameter("This demo only listens and connects on loopback")
    return str(address)


@app.command()
def server(
    host: str = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8765,
    seed: int | None = None,
    private: bool = False,
    dashboard: bool = True,
    auto_start_delay: float = 3600,
    action_timeout: float = 300,
) -> None:
    """Run the WebSocket game server with its public Textual console."""
    from pokerdemo.console.server import run_server
    from pokerdemo.game.service import PokerApplication, ServerConfig

    address = loopback_host(host)
    options = ServerConfig(
        seed=seed, auto_start_delay=auto_start_delay, action_timeout=action_timeout
    )
    run_server(PokerApplication(options, allow_private=private), address, port, dashboard=dashboard)


@app.command()
def client(
    player_id: Annotated[str, typer.Option()],
    seat: Annotated[int, typer.Option(min=0, max=9)],
    host: str = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8765,
    stack: Annotated[int, typer.Option(min=1)] = 1000,
    private: bool = False,
) -> None:
    """Open a small Textual player console; the server pushes table updates."""
    from pokerdemo.console.client import PlayerConsole
    from pokerdemo.protocol.models import Join

    Join(op="join", player_id=player_id, seat=seat, stack=stack)
    PlayerConsole(loopback_host(host), port, player_id, seat, stack, private).run()
