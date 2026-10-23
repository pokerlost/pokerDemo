"""A recipient-safe poker table with visible cards and a legal-action menu."""

from __future__ import annotations

import asyncio
import threading
from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Button, Footer, Input, Static

from pokerdemo.console.cards import CardsView, PlayerTableView
from pokerdemo.console.shared import command_request
from pokerdemo.console.wager import WagerDialog
from pokerdemo.network.client import ServerError, WebSocketTransport
from pokerdemo.protocol.models import Snapshot


class PlayerConsole(App):
    CSS_PATH = "client.tcss"
    AUTO_FOCUS = None
    ENABLE_COMMAND_PALETTE = False
    BINDINGS: ClassVar = [
        Binding("ctrl+q", "quit", "Exit"),
        Binding("escape", "table_focus", "Table", priority=True),
        Binding("colon", "command_focus", "Command", priority=True),
        Binding("enter", "enter_command", "Command", show=False, priority=True),
        Binding("s", "play('start')", "Deal", show=False),
        Binding("n", "play('start')", "Next", show=False),
        Binding("c", "play('check')", "Check", show=False),
        Binding("l", "play('call')", "Call", show=False),
        Binding("f", "play('fold')", "Fold", show=False),
        Binding("a", "play('all_in')", "All-in", show=False),
        Binding("d", "play('settle')", "Settle", show=False),
        Binding("b,r", "wager", "Bet / raise", show=False),
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
        yield Static(id="identity", markup=False)
        yield Static("Connecting to the table…", id="summary", markup=False)
        yield CardsView(id="cards")
        yield PlayerTableView(id="players")
        yield Static("Waiting for the table", id="turn", markup=False)
        with Horizontal(id="actions"):
            for key, label in (
                ("start", "S  Deal hand"),
                ("settle", "D  Reveal result"),
                ("fold", "F  Fold"),
                ("check", "C  Check"),
                ("call", "L  Call"),
                ("wager", "R  Raise…"),
                ("all-in", "A  All-in"),
            ):
                yield Button(label, id=key, disabled=True)
        yield Static("Click an action, or use Tab and Enter.", id="feedback", markup=False)
        yield Input(
            placeholder="Command: start / call / check / bet TOTAL / raise TOTAL", id="command"
        )
        yield Static(id="activity", markup=False)
        yield Footer()

    def on_resize(self) -> None:
        self.default_screen.set_class(self.size.height < 30, "compact")

    async def on_mount(self) -> None:
        self.set_focus(None)
        self.on_resize()
        self.query_one("#identity", Static).update(
            Text(
                f"♠  POKERDEMO    /    {self.player_id.upper()}  ·  SEAT {self.seat + 1}",
                style="bold #d8f6e7",
            )
        )
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
                "Choose an action below · click, or Tab then Enter."
            )
        except (ServerError, OSError) as error:
            self.query_one("#feedback", Static).update(f"Connection failed: {error}")

    async def on_unmount(self) -> None:
        self._leaving.set()
        if self.transport:
            await asyncio.to_thread(self.transport.close)

    def action_table_focus(self) -> None:
        self.query_one("#command", Input).display = False
        self.default_screen.remove_class("command-open")
        self.set_focus(None)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if isinstance(self.screen, WagerDialog):
            return action == "quit"
        if action == "enter_command":
            return self.focused is None
        if action == "command_focus":
            return not isinstance(self.focused, Input)
        return True

    def action_enter_command(self) -> None:
        self.action_command_focus()

    def action_command_focus(self) -> None:
        field = self.query_one("#command", Input)
        field.display = True
        self.default_screen.add_class("command-open")
        self.set_focus(field)

    def _enabled(self, key: str) -> bool:
        button = self.query_one(f"#{key}", Button)
        return button.display and not button.disabled

    def refresh_table(self) -> None:
        if self.transport is None:
            return
        if self.transport.error:
            self.feedback(self.transport.error + "; Ctrl+Q to exit", error=True)
            for button in self.query("#actions Button"):
                button.disabled = True
            return
        snapshot = self.transport.snapshot
        if snapshot is None or snapshot == self._last:
            return
        self._last = snapshot
        state = snapshot.state
        own = next((p for p in state.players if p.player_id == self.player_id), None)
        label = (state.street or state.phase).replace("_", " ").upper()
        title = Text(f"POT {state.pot:,}", style="bold #7bf0ba")
        title.append(f"    {label}    HAND {state.hand_id or '—'}", style="#a4bfba")
        title.append(f"\nYour stack  {own.stack:,}" if own else "\nTaking a seat…", style="#e7f1ec")
        if snapshot.result:
            result = ", ".join(f"{p.player_id} wins {p.amount:,}" for p in snapshot.result.payouts)
            title = Text(
                f"HAND COMPLETE  ·  {result}\nChoose Deal next hand to continue.",
                style="bold #7bf0ba",
            )
        self.query_one("#summary", Static).update(title)
        self.query_one(CardsView).show(snapshot, self.player_id)
        self.query_one(PlayerTableView).show(snapshot, self.player_id)
        legal = snapshot.legal_actions
        my_turn = state.current_player == self.player_id and state.phase == "betting"
        active = bool(my_turn and legal and not self._busy and not self.transport.error)
        for key in ("fold", "check", "call", "wager", "all-in"):
            button = self.query_one(f"#{key}", Button)
            button.display = state.phase == "betting"
            operation = key if key not in {"wager", "all-in"} else None
            allowed = (
                operation in legal.types
                if operation and legal
                else bool(legal and (legal.bet_to or legal.raise_to))
            )
            button.disabled = not (active and allowed)
        self.query_one("#call", Button).label = (
            f"L  Call {legal.call or 0:,}" if legal else "L  Call"
        )
        self.query_one("#wager", Button).label = (
            "R  Raise…" if legal and legal.raise_to else "B  Bet…"
        )
        start = self.query_one("#start", Button)
        start.display = state.phase in {"waiting", "ready", "finished"}
        start.disabled = (
            state.phase == "waiting"
            or self._busy
            or sum(p.stack > 0 and p.connected for p in state.players) < 2
        )
        start.label = "S  Deal next hand" if state.phase == "finished" else "S  Deal hand"
        settle = self.query_one("#settle", Button)
        settle.display = state.phase == "showdown"
        settle.disabled = self._busy
        turn = self.query_one("#turn", Static)
        turn.set_class(my_turn, "your-turn")
        if my_turn:
            message = (
                f"YOUR TURN  ·  {legal.call:,} to call"
                if legal and legal.call
                else "YOUR TURN  ·  you can check"
            )
        elif state.phase == "betting":
            message = f"Waiting for {state.current_player}  ·  actions unlock on your turn"
        elif state.phase == "showdown":
            message = "SHOWDOWN  ·  reveal the result when you are ready"
        elif start.disabled:
            message = "Waiting for two connected players with chips"
        else:
            message = "READY  ·  deal a hand to begin"
        turn.update(message)
        self.query_one("#activity", Static).update(
            "RECENT PLAY\n" + "\n".join(snapshot.activity[-3:])
        )

    def feedback(self, message: str, *, error: bool = False, success: bool = False) -> None:
        field = self.query_one("#feedback", Static)
        field.set_class(error, "error")
        field.set_class(success, "success")
        field.update(message)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        key = event.button.id
        if key == "wager":
            self.action_wager()
        elif key is not None:
            await self.action_play("all_in" if key == "all-in" else key)

    async def action_play(self, command: str) -> None:
        self.refresh_table()
        if self._enabled("all-in" if command == "all_in" else command):
            await self.execute(command)

    def action_wager(self) -> None:
        self.refresh_table()
        if not self._enabled("wager") or self._last is None:
            return
        snapshot = self._last
        legal = snapshot.legal_actions
        if legal is None:
            return
        action = "raise" if legal.raise_to else "bet"
        bounds = legal.raise_to or legal.bet_to
        own = next(p for p in snapshot.state.players if p.player_id == self.player_id)
        if bounds:
            self.push_screen(
                WagerDialog(
                    action,
                    bounds,
                    snapshot.state.version,
                    snapshot.state.pot,
                    own.street_committed,
                    legal.call or 0,
                ),
                self.finish_wager,
            )

    async def finish_wager(self, result: tuple[str, int, int] | None) -> None:
        self.set_focus(None)
        if result is None:
            return
        action, amount, version = result
        snapshot = self.transport.snapshot if self.transport else None
        if (
            snapshot is None
            or snapshot.state.version != version
            or snapshot.state.current_player != self.player_id
        ):
            self.feedback("Table changed. Choose an action from the updated menu.", error=True)
            return
        await self.execute(f"{action} {amount}")

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        command = event.value.strip().lstrip(":")
        event.input.value = ""
        self.action_table_focus()
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
        self._last = None
        self.refresh_table()
        try:
            request = command_request(command)
            await asyncio.to_thread(self.transport.request, request)
            self.feedback(f"OK [{command}]", success=True)
        except (ValueError, ServerError) as error:
            self.feedback(f"REJECTED [{command}]: {error}", error=True)
        finally:
            self._busy = False
            self._last = None
            self.refresh_table()
