"""Capture real console interactions and verify their source and engine provenance."""

from __future__ import annotations

import codecs
import fcntl
import hashlib
import importlib.metadata
import json
import os
import pty
import select
import struct
import subprocess
import termios
import time
import zipfile
from datetime import UTC, datetime
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import typer

from pokerdemo.network.client import PokerProtocol, WebSocketTransport
from pokerdemo.protocol.models import TableState
from pokerdemo.runtime.session import RECORDING_TERMINAL, SESSION, DemoSession
from pokerdemo.runtime.tools import resolve_wheel
from pokerdemo.settings import ENGINE_VERSION, Settings


def source_identity(workspace: Path) -> str:
    """Freeze a committed source tree without ignoring staged or unstaged edits."""
    source = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace, text=True).strip()
    branch = subprocess.check_output(
        ["git", "branch", "--show-current"], cwd=workspace, text=True
    ).strip()
    pushed = subprocess.check_output(
        ["git", "rev-parse", f"refs/remotes/origin/{branch}"], cwd=workspace, text=True
    ).strip()
    if branch not in {"dev", "main"} or source != pushed:
        raise RuntimeError("Recording requires the current dev/main commit to be pushed first")
    clean = subprocess.run(["git", "diff", "--quiet", "HEAD", "--"], cwd=workspace, check=False)
    if clean.returncode != 0:
        raise RuntimeError(
            "Recording requires a clean tracked source tree; commit and push dev first"
        )
    # The source receipt must describe the installed demo code actually executed.
    package = Path(__file__).resolve().parent.parent
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


class Recording:
    """Capture the actual tmux client PTY as an asciicast v2 stream."""

    def __init__(self, session: DemoSession, destination: Path, width: int, height: int) -> None:
        session.validate_paths()
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", height, width, 0, 0))
        # Match the owned recording apps; never change the user's terminal environment.
        environment = {**os.environ, **RECORDING_TERMINAL}
        environment.pop("NO_COLOR", None)
        self.process = subprocess.Popen(
            [session.tmux, "-S", str(session.socket), "attach-session", "-t", SESSION],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=environment,
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
                    "env": RECORDING_TERMINAL,
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


def record(settings: Settings, output: Path) -> None:
    """Record the same real apps with public or own-player synthetic client views."""
    workspace = settings.workspace.expanduser().absolute()
    wheel = resolve_wheel(settings)
    started_at = datetime.now(UTC).isoformat()
    source = source_identity(workspace)
    artifact = verify_wheel(wheel)
    session = DemoSession(settings, recording=True)
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
    manifest = session.start(public_capture=not settings.record_show_cards)
    capture: Recording | None = None
    transport: WebSocketTransport | None = None
    observations: list[dict[str, Any]] = []
    markers: dict[str, float] = {}
    completed = False
    try:
        capture = Recording(session, output, 160, 50)
        transport = WebSocketTransport("127.0.0.1", manifest["port"])
        protocol = PokerProtocol(transport)

        def public_state() -> TableState:
            state = protocol.state()
            if any(player.cards is not None for player in state.players):
                raise RuntimeError("Recording observer received private cards")
            return state

        def wait_for(predicate: Any, timeout: float = 12) -> TableState:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                capture.drain(0.1)
                state = public_state()
                if predicate(state):
                    return state
            raise RuntimeError("The real demo did not reach its expected state")

        def mark(name: str, *, pause: float | None = None) -> None:
            typer.echo(f"  Game: {name}")
            duration = delay if pause is None else pause
            capture.drain(duration)
            public_state()
            # Select a stable moment inside the display pause, before the next redraw.
            markers[name] = round(time.monotonic() - capture.started - duration / 2, 4)
            for role, pane in manifest["panes"].items():
                pane_capture = session.command("capture-pane", "-p", "-t", pane, check=False)
                if pane_capture.returncode == 0:
                    (output / f"{name}-{role}.txt").write_text(pane_capture.stdout)

        def submit(
            player: str, command: str, *, rejected: bool = False, wager_menu: bool = False
        ) -> TableState:
            before = public_state()
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
            if wager_menu:
                name, total = command.split()
                if name not in {"bet", "raise"} or rejected:
                    raise ValueError("Only a valid native wager can use the recording menu")
                pane_id = manifest["panes"][player]
                session.command("send-keys", "-t", pane_id, "Escape")
                # Textual's real terminal parser must resolve Escape before a letter
                # arrives, otherwise the bytes can become an Alt+letter sequence.
                capture.drain(0.25)
                session.command("send-keys", "-t", pane_id, "r" if name == "raise" else "b")
                menu_deadline = time.monotonic() + 12
                while time.monotonic() < menu_deadline:
                    capture.drain(0.1)
                    pane = session.command("capture-pane", "-p", "-t", pane_id).stdout
                    if name.upper() + " TO" in pane:
                        break
                else:
                    raise RuntimeError("The client did not open its wager menu")
                mark("wager-menu", pause=max(delay, 2.5))
                if public_state().version != before.version or protocol.events(0)[0] != cursor:
                    raise RuntimeError("Opening a wager menu changed the native game")
                # The same UI initializes the amount to its current native legal minimum.
                session.command("send-keys", "-t", pane_id, "Enter")
            else:
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
                state = public_state()
                after_cursor, _ = protocol.events(0)
                if state.version != before.version or cursor != after_cursor:
                    raise RuntimeError("Rejected input changed native state or event cursor")
            else:
                state = wait_for(lambda value: value.version != before.version)
                if wager_menu:
                    actor = next(p for p in state.players if p.player_id == player)
                    if actor.street_committed != int(total):
                        raise RuntimeError("The wager menu did not submit its native minimum")
                capture.drain(delay)
            observations.append(
                {
                    "hand": before.hand_id,
                    "street": before.street,
                    "player": player,
                    "command": command,
                    "input": "wager-menu" if wager_menu else "command",
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
            current = public_state().current_player
            if current is None:
                raise RuntimeError("The native engine has no current player")
            return submit(current, command)

        def minimum_action(name: str, *, wager_menu: bool = False) -> TableState:
            legal = protocol.legal()
            bounds = (
                legal.raise_to if name == "raise" and legal else legal.bet_to if legal else None
            )
            if bounds is None:
                raise RuntimeError(f"The native engine did not offer {name}")
            current = public_state().current_player
            if current is None:
                raise RuntimeError("The native engine has no current player")
            return submit(current, f"{name} {bounds.minimum}", wager_menu=wager_menu)

        wait_for(
            lambda state: (
                len(state.players) == 2 and set(state.connected_players) == {"alice", "bob"}
            )
        )
        mark("connected")
        submit("alice", "start")
        current = public_state().current_player
        if current is None:
            raise RuntimeError("Missing opening player")
        submit(current, "bet invalid", rejected=True)
        submit(current, "raise 1", rejected=True)
        other = "bob" if current == "alice" else "alice"
        submit(other, "call", rejected=True)
        minimum_action("raise", wager_menu=True)
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
            state = public_state()
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
            "schema": 2,
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
            "capture": "actual tmux client PTY; WebSocket server + two Textual clients",
            "privateViews": settings.record_show_cards,
            "views": {
                "server": "public",
                "observer": "public",
                "players": "own-player" if settings.record_show_cards else "public",
            },
            "syntheticPlayers": ["alice", "bob"],
            "terminal": RECORDING_TERMINAL,
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
    typer.echo(f"[1/3] Game captured; server and clients stopped. {output}")
