"""A compact player console: server-pushed state and a single command input."""

from __future__ import annotations

import asyncio
import threading
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, Input, Static

from pokerdemo.console.shared import CSS, TableView, command_request, summary
from pokerdemo.network.client import ServerError, WebSocketTransport
from pokerdemo.protocol.models import Snapshot


class PlayerConsole(App):
    CSS = CSS
    AUTO_FOCUS = None
    BINDINGS: ClassVar = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("escape", "table_focus", "Hotkeys"),
        Binding("s", "play('start')", "Start"),
        Binding("c", "play('check')", "Check"),
        Binding("l", "play('call')", "Call"),
        Binding("f", "play('fold')", "Fold"),
        Binding("a", "play('all_in')", "All-in"),
        Binding("d", "play('settle')", "Settle"),
        Binding("n", "play('start')", "Next"),
        Binding("q", "quit", "Quit", show=False),
        Binding("colon", "command_focus", "Command", show=False),
        Binding("enter", "command_focus", "Command", show=False),
    ]

    def __init__(
        self, host: str, port: int, player_id: str, seat: int, stack: int, private: bool
    ) -> None:
        super().__init__()
        self.title = f"PokerDemo · {player_id}"
        self.host, self.port = host, port
        self.player_id, self.seat, self.stack, self.private = player_id, seat, stack, private
        self.transport: WebSocketTransport | None = None
        self._last: Snapshot | None = None
        self._busy = False
        self._leaving = threading.Event()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Connecting…", id="summary", markup=False)
        yield TableView()
        yield Static(id="details", markup=False)
        yield Static(id="activity", markup=False)
        yield Static(
            "S starts the hand · : opens commands · Esc returns to hotkeys",
            id="feedback",
            markup=False,
        )
        yield Input(
            placeholder="start / check / call / fold / bet TOTAL / raise TOTAL / settle",
            id="command",
        )
        yield Footer()

    async def on_mount(self) -> None:
        self.set_focus(None)
        self.run_worker(self.connect(), exclusive=True)
        self.set_interval(0.1, self.refresh_table)

    def _connect_transport(self) -> WebSocketTransport:
        transport = WebSocketTransport(self.host, self.port)
        self.transport = transport
        # A cancelled to_thread await cannot stop its constructor. Close a
        # connection that finishes opening after the console has already exited.
        if self._leaving.is_set():
            transport.close()
            raise ServerError("Console closed while connecting")
        return transport

    async def connect(self) -> None:
        try:
            self.transport = await asyncio.to_thread(self._connect_transport)
            await asyncio.to_thread(
                self.transport.request,
                {"op": "join", "player_id": self.player_id, "seat": self.seat, "stack": self.stack},
            )
            await asyncio.to_thread(self.transport.subscribe, private=self.private)
            self.query_one("#feedback", Static).update(
                "Connected. Press S to start, or : for a command."
            )
        except (ServerError, OSError) as error:
            self.query_one("#feedback", Static).update(f"Connection failed: {error}")

    async def on_unmount(self) -> None:
        self._leaving.set()
        if self.transport:
            await asyncio.to_thread(self.transport.close)

    def action_table_focus(self) -> None:
        self.set_focus(None)

    def action_command_focus(self) -> None:
        self.query_one(Input).focus()

    def refresh_table(self) -> None:
        if self.transport is None:
            return
        if self.transport.error:
            self.query_one("#feedback", Static).update(self.transport.error + "; Ctrl+Q to exit")
        snapshot = self.transport.snapshot
        if snapshot is None or snapshot == self._last:
            return
        self._last = snapshot
        state = snapshot.state
        self.query_one("#summary", Static).update(summary(snapshot))
        self.query_one(TableView).show(snapshot)
        own = next((p for p in state.players if p.player_id == self.player_id), None)
        cards = (
            " ".join(own.cards)
            if own and own.cards
            else ("not dealt" if self.private else "hidden (public recording)")
        )
        detail = f"YOUR CARDS: {cards}\n"
        if state.phase in {"ready", "finished", "waiting"}:
            detail += "S: start / next hand"
        elif state.phase == "showdown":
            detail += "D: settle showdown"
        elif state.current_player == self.player_id and snapshot.legal_actions:
            legal = snapshot.legal_actions
            detail += "YOUR TURN: " + ", ".join(legal.types)
            for name, bounds in (("bet", legal.bet_to), ("raise", legal.raise_to)):
                if bounds:
                    detail += f" | {name} {bounds.minimum}..{bounds.maximum}"
        else:
            detail += f"WAITING: {state.current_player or '-'}"
        self.query_one("#details", Static).update(detail)
        self.query_one("#activity", Static).update("\n".join(snapshot.activity[-5:]))

    async def action_play(self, command: str) -> None:
        await self.execute(command)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        command = event.value.strip().lstrip(":")
        event.input.value = ""
        self.set_focus(None)
        await self.execute(command)

    async def execute(self, command: str) -> None:
        if command in {"quit", "q"}:
            self.exit()
            return
        if command == "help":
            self.query_one("#feedback", Static).update(
                "start · check/call/fold/all_in · bet TOTAL · raise TOTAL · settle · quit"
            )
            return
        if self.transport is None or self._busy:
            return
        self._busy = True
        try:
            request = command_request(command)
            await asyncio.to_thread(self.transport.request, request)
            self.query_one("#feedback", Static).update(f"OK [{command}]")
        except (ValueError, ServerError) as error:
            self.query_one("#feedback", Static).update(f"REJECTED [{command}]: {error}")
        finally:
            self._busy = False
