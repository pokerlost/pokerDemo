"""Public server console built from Textual widgets, independent of WebSocket I/O."""

from __future__ import annotations

import threading
from typing import ClassVar

from pokerengine import PokerError
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, Input, Static

from pokerdemo.console.shared import CSS, TableView, summary
from pokerdemo.game.service import PokerApplication
from pokerdemo.network.server import PokerWebSocketServer
from pokerdemo.protocol.models import Snapshot


class ServerConsole(App):
    CSS = CSS + "\nInput, Input:focus { height: 1; border: none; padding: 0 1; }"
    TITLE = "PokerDemo · WebSocket server"
    BINDINGS: ClassVar = [
        Binding("ctrl+q", "quit", "Stop server"),
        Binding("escape", "focus_command", "Command"),
    ]

    def __init__(self, server: PokerWebSocketServer) -> None:
        super().__init__()
        self.server = server
        self.game: PokerApplication = server.application
        self._last: Snapshot | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="summary", markup=False)
        yield TableView()
        host, port = self.server.server_address
        yield Static(
            f"ws://{host}:{port}/ws · public monitor · status / start / settle / quit",
            id="details",
            markup=False,
        )
        yield Static(id="activity", markup=False)
        yield Static("Ready. Players press S, or type start below.", id="feedback", markup=False)
        yield Input(placeholder="Server command: status / start / settle / quit", id="command")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(0.1, self.refresh_table)
        self.refresh_table()
        self.query_one(Input).focus()

    def action_focus_command(self) -> None:
        self.query_one(Input).focus()

    def refresh_table(self) -> None:
        snapshot = Snapshot(**self.game.snapshot(None, 0))
        if snapshot == self._last:
            return
        self._last = snapshot
        self.query_one("#summary", Static).update(summary(snapshot))
        self.query_one(TableView).show(snapshot)
        self.query_one("#activity", Static).update("\n".join(snapshot.activity[-5:]))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        command = event.value.strip().lower()
        event.input.value = ""
        if command in {"quit", "q", "stop"}:
            self.exit()
            return
        try:
            if command in {"start", "settle"}:
                self.game.operator_command(command)
                self.server.publish()
                message = f"OK: {command}"
            elif command == "status":
                message = (
                    f"{len(self.game.dashboard_snapshot()[1])} players connected; "
                    "WebSocket listener running"
                )
            else:
                message = (
                    "Commands: status, start, settle, quit. Game actions belong to player panels."
                )
            self.query_one("#feedback", Static).update(message)
        except (ValueError, RuntimeError, PokerError) as error:
            self.query_one("#feedback", Static).update(f"Rejected: {error}")


def run_server(application: PokerApplication, host: str, port: int, *, dashboard: bool) -> None:
    with PokerWebSocketServer((host, port), application) as server:
        if dashboard:
            worker = threading.Thread(
                target=server.serve_forever, name="websocket-listener", daemon=True
            )
            worker.start()
            try:
                ServerConsole(server).run()
            finally:
                server.shutdown()
                worker.join(timeout=3)
        else:
            print(
                f"PokerDemo WebSocket server: ws://{host}:{server.server_address[1]}/ws", flush=True
            )
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
