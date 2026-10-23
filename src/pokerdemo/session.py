"""Own a dedicated tmux session; poker decisions remain in the native engine."""

from __future__ import annotations

import codecs
import fcntl
import hashlib
import importlib.metadata
import json
import os
import pty
import select
import shlex
import socket
import stat
import struct
import subprocess
import sys
import termios
import time
import zipfile
from datetime import UTC, datetime
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import typer

from pokerdemo.client import PokerProtocol, TableState, TCPTransport
from pokerdemo.settings import ENGINE_VERSION, Settings
from pokerdemo.tools import resolve_tool, resolve_wheel

SESSION = "pokerdemo"


def source_identity(workspace: Path) -> str:
    """Freeze a committed source tree without ignoring staged or unstaged edits."""
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace, text=True).strip()
    branch = subprocess.check_output(
        ["git", "branch", "--show-current"], cwd=workspace, text=True
    ).strip()
    pushed = subprocess.check_output(
        ["git", "rev-parse", "refs/remotes/origin/dev"], cwd=workspace, text=True
    ).strip()
    if branch != "dev" or source != pushed:
        raise RuntimeError("Recording requires the current dev commit to be pushed first")
    clean = subprocess.run(["git", "diff", "--quiet", "HEAD", "--"], cwd=workspace, check=False)
    if clean.returncode != 0:
        raise RuntimeError(
            "Recording requires a clean tracked source tree; commit and push dev first"
        )
    # The source receipt must describe the installed demo code actually executed.
    package = Path(__file__).resolve().parent
    paths = subprocess.check_output(
        ["git", "ls-tree", "-r", "--name-only", "HEAD", "src/pokerdemo"],
        cwd=workspace,
        text=True,
    ).splitlines()
    if not paths:
        raise RuntimeError("Committed demo package is missing from the workspace")
    for name in paths:
        expected = subprocess.check_output(["git", "show", f"HEAD:{name}"], cwd=workspace)
        installed = package / Path(name).relative_to("src/pokerdemo")
        if not installed.is_file() or installed.read_bytes() != expected:
            raise RuntimeError(
                "Installed demo package differs from committed source; reinstall it explicitly"
            )
    return source


def verify_wheel(wheel: Path) -> dict[str, Any]:
    """Match the supplied wheel to source metadata and installed package bytes."""
    import pokerengine

    distribution = importlib.metadata.distribution("pokerengine")
    source_version = ENGINE_VERSION
    package_root = Path(pokerengine.__file__).resolve().parent
    installed_root = Path(distribution.locate_file("pokerengine/__init__.py")).resolve().parent
    if package_root != installed_root:
        raise RuntimeError("Imported pokerengine does not belong to the installed distribution")
    hashes: dict[str, str] = {}
    native: dict[str, str] = {}
    with zipfile.ZipFile(wheel) as archive:
        metadata_files = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(metadata_files) != 1:
            raise RuntimeError("Expected exactly one wheel metadata file")
        metadata = BytesParser().parsebytes(archive.read(metadata_files[0]))
        version = metadata.get("Version")
        if (
            metadata.get("Name") != "pokerengine"
            or version != distribution.version
            or version != source_version
        ):
            raise RuntimeError(
                "Wheel, installed distribution and committed source versions must match"
            )
        for name in archive.namelist():
            if not name.startswith("pokerengine/") or name.endswith("/"):
                continue
            relative = Path(name).relative_to("pokerengine")
            if ".." in relative.parts:
                raise RuntimeError("Invalid package path in wheel")
            installed = package_root / relative
            expected = hashlib.sha256(archive.read(name)).hexdigest()
            if (
                not installed.is_file()
                or hashlib.sha256(installed.read_bytes()).hexdigest() != expected
            ):
                raise RuntimeError(f"Installed package differs from supplied wheel: {name}")
            hashes[name] = expected
            if relative.name.startswith("_core") and relative.suffix in {".so", ".pyd", ".dylib"}:
                native[name] = expected
    if not native:
        raise RuntimeError("Supplied wheel has no verified installed native extension")
    return {
        "wheelVersion": distribution.version,
        "wheelFile": wheel.name,
        "wheelSha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "installedPackageSha256": hashes,
        "installedNativeSha256": native,
        "verification": (
            "Wheel metadata matches source/installed versions; "
            "all installed package bytes match wheel"
        ),
    }


class DemoSession:
    """Manage only this repository's dedicated tmux socket and session."""

    def __init__(self, settings: Settings) -> None:
        self.tool = resolve_tool("tmux", settings.tmux)
        self.tmux = self.tool.executable
        self.root = settings.workspace.expanduser().absolute()
        repository = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], cwd=self.root, text=True
        ).strip()
        if repository != str(self.root):
            raise ValueError("Workspace must be this demo's exact repository root")
        self.state = self.root / ".local/demo"
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

    def start(self, *, public_capture: bool, width: int = 160, height: int = 50) -> dict[str, Any]:
        if self.command("has-session", "-t", SESSION, check=False).returncode == 0:
            raise typer.BadParameter(
                "Demo already exists: attach with make demo-attach or stop with make demo-stop"
            )
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        server = [
            sys.executable,
            "-u",
            "-m",
            "pokerdemo.server",
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
            "exec " + shlex.join(server),
        ).stdout.strip()
        try:
            self.command("set-option", "-g", "status", "off")
            self.command("set-option", "-g", "pane-border-status", "top")
            self.command("set-option", "-g", "pane-border-format", " #{pane_title} ")
            self.command("select-pane", "-t", first, "-T", "PUBLIC SERVER")
            # Wait for the real server greeting before starting either player.
            ready = TCPTransport("127.0.0.1", port)
            ready.close()
            panes: dict[str, str] = {"server": first}
            for seat, name in enumerate(("alice", "bob")):
                client = [
                    sys.executable,
                    "-u",
                    "-m",
                    "pokerdemo.client",
                    "--player-id",
                    name,
                    "--seat",
                    str(seat),
                    "--port",
                    str(port),
                ]
                if not public_capture:
                    client.append("--private")
                options = ["-v", "-l", "65%"] if seat == 0 else ["-h", "-l", "50%"]
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
                    "exec " + shlex.join(client),
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


class Recording:
    """Capture the actual tmux client PTY as an asciicast v2 stream."""

    def __init__(self, session: DemoSession, destination: Path, width: int, height: int) -> None:
        session.validate_paths()
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
        # This is the declared terminal type of a fresh recording PTY, not a host font fix.
        self.process = subprocess.Popen(
            [session.tmux, "-S", str(session.socket), "attach-session", "-t", SESSION],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={**os.environ, "TERM": "xterm-256color"},
            start_new_session=True,
        )
        os.close(slave)
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.started = time.monotonic()
        self.stream = (destination / "session.cast").open("w")
        self.raw = (destination / "tmux.typescript").open("wb")
        self.stream.write(
            json.dumps(
                {
                    "version": 2,
                    "width": width,
                    "height": height,
                    "title": "PokerDemo: actual three-process tmux session",
                    "env": {"TERM": "xterm-256color"},
                }
            )
            + "\n"
        )

    def drain(self, seconds: float = 0.1) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            ready, _, _ = select.select(
                [self.master], [], [], min(0.05, max(0, deadline - time.monotonic()))
            )
            if ready:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    break
                if not data:
                    break
                self.raw.write(data)
                self.stream.write(
                    json.dumps(
                        [
                            round(time.monotonic() - self.started, 4),
                            "o",
                            self.decoder.decode(data),
                        ]
                    )
                    + "\n"
                )
                self.stream.flush()

    def close(self) -> None:
        self.drain(0.3)
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        os.close(self.master)
        self.stream.close()
        self.raw.close()


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


def record(settings: Settings, output: Path) -> None:
    """Record a separate public-only, scripted run of the same real curses apps."""
    workspace = settings.workspace.expanduser().absolute()
    wheel = resolve_wheel(settings)
    started_at = datetime.now(UTC).isoformat()
    source = source_identity(workspace)
    artifact = verify_wheel(wheel)
    session = DemoSession(settings)
    tool_before = session.tool.describe()
    delay = settings.delay
    # No symlinked destination or overwritten run: all outputs are private local evidence.
    for candidate in (output, *output.parents):
        if candidate.is_symlink():
            raise ValueError("Recording destination must not traverse symlinks")
    output = output.resolve()
    recording_root = workspace / ".local/recordings"
    if not output.is_relative_to(recording_root):
        raise ValueError("Recordings must remain beneath this workspace .local/recordings")
    if output.is_relative_to(workspace):
        ignored = subprocess.run(
            ["git", "check-ignore", "--quiet", str(output / "session.cast")],
            cwd=workspace,
            check=False,
        )
        if ignored.returncode != 0:
            raise ValueError("Recording destination inside the repository must be ignored")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    output.chmod(0o700)
    manifest = session.start(public_capture=True)
    capture: Recording | None = None
    transport: TCPTransport | None = None
    observations: list[dict[str, Any]] = []
    markers: dict[str, float] = {}
    completed = False
    try:
        capture = Recording(session, output, 160, 50)
        transport = TCPTransport("127.0.0.1", manifest["port"])
        protocol = PokerProtocol(transport)

        def wait_for(predicate: Any, timeout: float = 12) -> TableState:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                capture.drain(0.1)
                state = protocol.state()
                if predicate(state):
                    return state
            raise RuntimeError("The real demo did not reach its expected state")

        def mark(name: str) -> None:
            capture.drain(delay)
            markers[name] = round(time.monotonic() - capture.started, 4)
            for role, pane in manifest["panes"].items():
                pane_capture = session.command("capture-pane", "-p", "-t", pane, check=False)
                if pane_capture.returncode == 0:
                    (output / f"{name}-{role}.txt").write_text(pane_capture.stdout)

        def submit(player: str, command: str, *, rejected: bool = False) -> TableState:
            before = protocol.state()
            cursor, _ = protocol.events(0)
            rejection_marker = f"REJECTED [{command}]"
            if rejected:
                baseline = session.command(
                    "capture-pane", "-p", "-t", manifest["panes"][player]
                ).stdout
                if rejection_marker in baseline:
                    raise RuntimeError(
                        "Recording rejection commands must have unique visible markers"
                    )
            session.send(manifest["panes"][player], command)
            if rejected:
                rejection_deadline = time.monotonic() + 12
                while time.monotonic() < rejection_deadline:
                    capture.drain(0.1)
                    pane = session.command(
                        "capture-pane", "-p", "-t", manifest["panes"][player]
                    ).stdout
                    if rejection_marker in pane:
                        break
                else:
                    raise RuntimeError(f"The client did not display this rejection: {command}")
                capture.drain(delay)
                state = protocol.state()
                after_cursor, _ = protocol.events(0)
                if state.version != before.version or cursor != after_cursor:
                    raise RuntimeError("Rejected input changed native state or event cursor")
            else:
                state = wait_for(lambda value: value.version != before.version)
                capture.drain(delay)
            observations.append(
                {
                    "hand": before.hand_id,
                    "street": before.street,
                    "player": player,
                    "command": command,
                    "rejected": rejected,
                    "rejectionMarker": rejection_marker if rejected else None,
                    "beforeVersion": before.version,
                    "afterVersion": state.version,
                    "phase": state.phase,
                    "board": list(state.board),
                    "pot": state.pot,
                }
            )
            return state

        def action(command: str) -> TableState:
            current = protocol.state().current_player
            if current is None:
                raise RuntimeError("The native engine has no current player")
            return submit(current, command)

        def minimum_action(name: str) -> TableState:
            legal = protocol.legal()
            bounds = (
                legal.raise_to if name == "raise" and legal else legal.bet_to if legal else None
            )
            if bounds is None:
                raise RuntimeError(f"The native engine did not offer {name}")
            return action(f"{name} {bounds[0]}")

        wait_for(
            lambda state: (
                len(state.players) == 2 and set(state.connected_players) == {"alice", "bob"}
            )
        )
        mark("connected")
        submit("alice", "start")
        current = protocol.state().current_player
        if current is None:
            raise RuntimeError("Missing opening player")
        submit(current, "bet invalid", rejected=True)
        submit(current, "raise 1", rejected=True)
        other = "bob" if current == "alice" else "alice"
        submit(other, "call", rejected=True)
        minimum_action("raise")
        action("call")
        mark("flop")
        minimum_action("bet")
        minimum_action("raise")
        action("call")
        mark("turn")
        action("check")
        action("check")
        mark("river")
        minimum_action("bet")
        action("call")
        wait_for(lambda state: state.phase == "showdown")
        mark("showdown")
        submit("alice", "settle")
        mark("result")
        observations.append({"scenario": "streets-showdown", "result": protocol.result()["result"]})
        submit("alice", "start")
        action("fold")
        mark("fold-result")
        observations.append({"scenario": "fold", "result": protocol.result()["result"]})
        submit("alice", "start")
        action("all_in")
        action("call")
        # Unequal stacks can leave a native current player on later streets.
        # Submit only the engine's offered passive action; do not assume runout.
        for _ in range(16):
            state = protocol.state()
            if state.phase == "showdown":
                break
            legal = protocol.legal()
            if state.phase != "betting" or legal is None:
                raise RuntimeError("All-in scenario has no native continuation to showdown")
            continuation = next((name for name in ("check", "call") if name in legal.types), None)
            if continuation is None:
                raise RuntimeError("The native engine offered no passive continuation")
            action(continuation)
        else:
            raise RuntimeError("All-in scenario exceeded its bounded native-action loop")
        mark("all-in-showdown")
        submit("alice", "settle")
        mark("all-in-result")
        observations.append({"scenario": "all-in", "result": protocol.result()["result"]})
        session.send(manifest["panes"]["bob"], "quit")
        disconnected = wait_for(lambda state: "bob" not in state.connected_players)
        observations.append(
            {
                "scenario": "disconnect",
                "player": "bob",
                "connectedPlayers": list(disconnected.connected_players),
            }
        )
        mark("disconnected")
        completed = True
    finally:
        if transport:
            transport.close()
        session.stop()
        if capture:
            capture.close()
        verification_error = ""
        try:
            source_after = source_identity(workspace)
            unchanged = (
                source_after == source
                and verify_wheel(wheel) == artifact
                and session.tool.describe() == tool_before
            )
        except (RuntimeError, OSError, zipfile.BadZipFile) as error:
            source_after = None
            unchanged = False
            verification_error = str(error)
        completed = completed and unchanged
        receipt = {
            "schema": 1,
            "startedAtUtc": started_at,
            "captureFinishedAtUtc": datetime.now(UTC).isoformat(),
            "pipelineCompleted": False,
            "renderCompleted": False,
            "sourceCommit": source,
            "demoVersion": importlib.metadata.version("pokerdemo"),
            "tools": {"tmux": tool_before},
            **artifact,
            "sourceCommitAfter": source_after,
            "sourceAndArtifactUnchanged": unchanged,
            "verificationError": verification_error,
            "capture": "actual tmux client PTY; curses server + two curses clients",
            "privateViews": False,
            "manualModeHasOwnPlayerView": True,
            "loopbackOnly": True,
            "completed": completed,
            "markers": markers,
            "observations": observations,
        }
        (output / "recording.json").write_text(json.dumps(receipt, indent=2) + "\n")
    if not completed:
        raise RuntimeError(
            "Source or installed wheel changed during the recording; receipt marked incomplete"
        )
    typer.echo(f"Actual public-only tmux capture saved to {output}; no tests/build were invoked.")
