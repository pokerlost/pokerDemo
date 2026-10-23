"""Own a dedicated tmux session; poker decisions remain in the native engine."""

from __future__ import annotations

import json
import os
import shlex
import socket
import stat
import subprocess
import sys
from typing import Any

import typer

from pokerdemo.network.client import WebSocketTransport
from pokerdemo.runtime.tools import resolve_tool
from pokerdemo.settings import Settings

SESSION = "pokerdemo"
RECORDING_TERMINAL = {
    "TERM": "xterm-256color",
    "COLORTERM": "truecolor",
    "TEXTUAL_COLOR_SYSTEM": "truecolor",
    "ESCDELAY": "100",
}


class DemoSession:
    """Manage only this repository's dedicated tmux socket and session."""

    def __init__(self, settings: Settings, *, recording: bool = False) -> None:
        self.tool = resolve_tool("tmux", settings.tmux)
        self.tmux = self.tool.executable
        self.root = settings.workspace.expanduser().absolute()
        repository = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], cwd=self.root, text=True
        ).strip()
        if repository != str(self.root):
            raise ValueError("Workspace must be this demo's exact repository root")
        self.recording = recording
        self.state = self.root / (".local/demo-record" if recording else ".local/demo")
        self.socket = self.state / "tmux.sock"
        self.manifest = self.state / "session.json"

    def validate_paths(self) -> None:
        """Guard every tmux operation, including attach/stop, before touching its socket."""
        for path in (self.root / ".local", self.state, self.socket, self.manifest):
            if path.is_symlink():
                raise typer.BadParameter(f"Refusing symlinked demo runtime path: {path}")
            if not path.exists():
                continue
            info = path.stat()
            expected = (
                stat.S_ISSOCK
                if path == self.socket
                else stat.S_ISREG
                if path == self.manifest
                else stat.S_ISDIR
            )
            if info.st_uid != os.getuid() or not expected(info.st_mode):
                raise typer.BadParameter(f"Unexpected demo runtime path owner/type: {path}")

    def command(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        self.validate_paths()
        return subprocess.run(
            [self.tmux, "-f", "/dev/null", "-S", str(self.socket), *arguments],
            check=check,
            capture_output=True,
            text=True,
        )

    def application_command(self, arguments: list[str]) -> str:
        """Declare colors only for owned recording apps, regardless of host NO_COLOR."""
        if self.recording:
            arguments = [
                "env",
                "-u",
                "NO_COLOR",
                *(f"{name}={value}" for name, value in RECORDING_TERMINAL.items()),
                *arguments,
            ]
        return "exec " + shlex.join(arguments)

    def start(self, *, public_capture: bool, width: int = 160, height: int = 50) -> dict[str, Any]:
        if self.command("has-session", "-t", SESSION, check=False).returncode == 0:
            raise typer.BadParameter(
                "Recording already running; wait or interrupt that recording command"
                if self.recording
                else "Demo already exists: attach with make demo-attach or stop with make demo-stop"
            )
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = [
            sys.executable,
            "-u",
            "-m",
            "pokerdemo",
            "server",
            "--dashboard",
            "--port",
            str(port),
            "--seed",
            "42",
            "--auto-start-delay",
            "3600",
            "--action-timeout",
            "300",
            "--private",
        ]
        first = self.command(
            "new-session",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-s",
            SESSION,
            "-x",
            str(width),
            "-y",
            str(height),
            "-c",
            str(self.root),
            self.application_command(server),
        ).stdout.strip()
        try:
            self.command("set-option", "-g", "status", "off")
            self.command("set-option", "-g", "mouse", "on")
            self.command("set-option", "-g", "pane-border-status", "top")
            self.command("set-option", "-g", "pane-border-format", " #{pane_title} ")
            self.command("select-pane", "-t", first, "-T", "PUBLIC SERVER")
            # Wait for the real server greeting before starting either player.
            ready = WebSocketTransport("127.0.0.1", port)
            ready.close()
            panes: dict[str, str] = {"server": first}
            for seat, name in enumerate(("alice", "bob")):
                client = [
                    sys.executable,
                    "-u",
                    "-m",
                    "pokerdemo",
                    "client",
                    "--player-id",
                    name,
                    "--seat",
                    str(seat),
                    "--port",
                    str(port),
                ]
                if not public_capture:
                    client.append("--private")
                split_size = "70%" if self.recording else "60%"
                options = ["-v", "-l", split_size] if seat == 0 else ["-h", "-l", "50%"]
                target = first if seat == 0 else panes["alice"]
                panes[name] = self.command(
                    "split-window",
                    "-d",
                    "-P",
                    "-F",
                    "#{pane_id}",
                    "-t",
                    target,
                    *options,
                    "-c",
                    str(self.root),
                    self.application_command(client),
                ).stdout.strip()
                self.command("select-pane", "-t", panes[name], "-T", name.upper())
            self.command("select-pane", "-t", panes["alice"])
            manifest = {
                "session": SESSION,
                "socket": str(self.socket),
                "port": port,
                "panes": panes,
                "publicCapture": public_capture,
            }
            self.manifest.write_text(json.dumps(manifest, indent=2) + "\n")
            return manifest
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        self.command("kill-session", "-t", SESSION, check=False)
        self.manifest.unlink(missing_ok=True)

    def attach(self) -> None:
        self.validate_paths()
        subprocess.run(
            [self.tmux, "-S", str(self.socket), "attach-session", "-t", SESSION], check=True
        )

    def send(self, pane: str, command: str) -> None:
        self.command("send-keys", "-t", pane, "-l", "--", ":" + command)
        self.command("send-keys", "-t", pane, "Enter")


def launch(settings: Settings, *, detach: bool = False) -> None:
    """Open the server above two independent human-controlled clients."""
    session = DemoSession(settings)
    session.start(public_capture=False)
    if not detach:
        try:
            session.attach()
        except BaseException:
            session.stop()
            raise
    typer.echo("Detached. Use make demo-attach to return; make demo-stop stops only this demo.")
