#!/usr/bin/env python3
"""Small newline-delimited JSON TCP server for the PokerEngine example.

The default protocol is public-only and has no TLS or authentication. The
server owns timeout fallback policy: a disconnected or inactive current player
is checked when possible, otherwise folded.
"""

from __future__ import annotations

import curses
import ipaddress
import json
import math
import socketserver
import sys
import threading
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from typing import Annotated

import typer
from pokerengine import Action, ActionType, AmountRange, CommandRecord, GameView, PokerGame


def _json_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("request must be a JSON object")
    result: dict[str, object] = {}
    for key, item in value.items():
        if type(key) is not str:
            raise ValueError("request contains a non-string object key")
        result[key] = item
    return result


def _write_line(value: str = "") -> None:
    sys.stdout.write(value + "\n")
    sys.stdout.flush()


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """Configuration for the example server and its initial table."""

    small_blind: int = 50
    big_blind: int = 100
    seed: int | None = None
    minimum_players: int = 2
    maximum_players: int = 10
    auto_start_delay: float = 15.0
    action_timeout: float = 8.0
    minimum_buy_in: int = 100
    maximum_buy_in: int = 10_000
    admin_token: str | None = None


@dataclass(slots=True)
class PlayerSession:
    """Mutable connection metadata for one seated player."""

    connection_id: int
    last_activity: float


class PokerApplication:
    """Thread-safe game service with reconnect and timeout fallback policies."""

    def __init__(self, config: ServerConfig, *, allow_private: bool = False) -> None:
        self._lock = threading.RLock()
        self.allow_private = allow_private
        self.minimum_players = config.minimum_players
        self.maximum_players = config.maximum_players
        self.auto_start_delay = config.auto_start_delay
        self.action_timeout = config.action_timeout
        self.minimum_buy_in = config.minimum_buy_in
        self.maximum_buy_in = config.maximum_buy_in
        self.admin_token = config.admin_token
        self._seed = config.seed
        self._sessions: dict[str, PlayerSession] = {}
        self.activity: deque[str] = deque(maxlen=100)
        self._turn: tuple[str | None, int] | None = None
        self._turn_started = time.monotonic()
        self._auto_start_deadline: float | None = None
        self.game = PokerGame.texas_holdem_no_limit(
            small_blind=config.small_blind,
            big_blind=config.big_blind,
            minimum_players=config.minimum_players,
            maximum_players=config.maximum_players,
            seed=config.seed,
        )

    def handle(
        self,
        request: dict[str, object],
        identity: str | None,
        connection_id: int,
    ) -> tuple[dict[str, object], str | None]:
        """Apply one request and return ``(response, connection_identity)``."""
        operation = request.get("op")
        if not isinstance(operation, str):
            raise ValueError("request field 'op' must be a string")

        with self._lock:
            if identity is not None:
                self._touch(identity, connection_id)
            if operation == "join":
                player_id = self._string(request, "player_id")
                if len(player_id) > 32 or not player_id.isprintable():
                    raise ValueError("player_id must be 1-32 printable characters")
                if identity is not None and identity != player_id:
                    raise PermissionError("a connection can control only its bound player")
                existing = next(
                    (
                        player
                        for player in self.game.public_view().players
                        if player.player_id == player_id
                    ),
                    None,
                )
                if existing is not None:
                    if existing.seat != self._integer(request, "seat"):
                        raise ValueError("resume seat does not match table seat")
                    # This example has no authentication: a reconnect explicitly
                    # takes over the previous local session for the same player ID.
                    self._sessions[player_id] = PlayerSession(connection_id, time.monotonic())
                    self._reset_auto_start()
                    self.activity.append(f"CONNECTED {player_id} (resumed)")
                    return self._ack(player_id, resumed=True), player_id
                record = self.game.join(
                    player_id,
                    seat=self._integer(request, "seat"),
                    stack=self._buy_in(request),
                )
                self._sessions[player_id] = PlayerSession(connection_id, time.monotonic())
                self._reset_auto_start()
                self.activity.append(f"JOIN {player_id} seat {self._integer(request, 'seat')}")
                return self._record(record, player_id), player_id

            if operation == "leave":
                player_id = self._require_identity(identity)
                self._sessions.pop(player_id, None)
                self._reset_auto_start()
                self.activity.append(f"DISCONNECTED {player_id}")
                return {"ok": True, "identity": player_id, "away": True, "events": []}, None

            if operation == "start":
                self._require_identity(identity)
                record = self.game.start_hand()
                self._auto_start_deadline = None
                self.activity.append(f"HAND {self.game.public_view().hand_id} started")
                return self._record(record, identity), identity

            if operation == "settle":
                self._require_identity(identity)
                record = self.game.settle_showdown()
                self.activity.append("SHOWDOWN settled; N starts next hand")
                return self._record(record, identity), identity

            if operation == "result":
                settlement = self.game.settlement
                return {
                    "ok": True,
                    "result": None
                    if settlement is None
                    else {
                        "payouts": [
                            {"player_id": payout.player_id, "amount": payout.amount}
                            for payout in settlement.payouts
                        ],
                        "rake": settlement.rake,
                    },
                }, identity

            if operation == "action":
                player_id = self._require_identity(identity)
                if self.game.current_player != player_id:
                    raise PermissionError("it is not this connection's turn")
                action = self._action(request)
                record = self.game.act(action)
                total = f" {action.total}" if action.total is not None else ""
                self.activity.append(f"{player_id}: {action.type.value}{total}")
                self._touch(player_id, connection_id)
                return self._record(record, player_id), player_id

            if operation == "legal":
                legal = self.game.legal_actions
                return {
                    "ok": True,
                    "legal_actions": (
                        None
                        if legal is None
                        else {
                            "types": [item.value for item in legal.types],
                            "call": legal.call_amount,
                            "bet_to": self._range(legal.bet_to),
                            "raise_to": self._range(legal.raise_to),
                        }
                    ),
                }, identity

            if operation in ("state", "public_state"):
                return {"ok": True, "state": self._public_view()}, identity

            if operation == "private_state":
                player_id = self._require_identity(identity)
                if not self.allow_private:
                    raise PermissionError(
                        "private state is disabled; enable loopback private mode in server setup"
                    )
                return {"ok": True, "state": self._player_view(player_id)}, player_id

            if operation == "admin_create_game":
                self._require_admin(request)
                self._create_game(request)
                return {"ok": True, "admin": True, "events": []}, identity

            if operation == "events":
                cursor = self._integer(request, "cursor", minimum=0)
                events = self.game.events_since(cursor)
                return {
                    "ok": True,
                    "cursor": self.game.event_cursor,
                    "events": [type(event).__name__ for event in events],
                }, identity

            raise ValueError(f"unknown operation: {operation}")

    def note_rejection(self, identity: str | None, category: str) -> None:
        """Keep a public diagnostic without request payloads, tokens or trusted views."""
        with self._lock:
            self.activity.append(f"REJECTED {identity or 'unjoined'}: {category}")

    def dashboard_snapshot(self) -> tuple[GameView, frozenset[str], list[str], str]:
        """Copy only public data while holding the application's mutation lock."""
        with self._lock:
            settlement = self.game.settlement
            result = ""
            if settlement is not None:
                result = (
                    "RESULT "
                    + ", ".join(
                        f"{payout.player_id} +{payout.amount}" for payout in settlement.payouts
                    )
                    + f" | rake {settlement.rake}"
                )
            return self.game.public_view(), frozenset(self._sessions), list(self.activity), result

    def _require_admin(self, request: dict[str, object]) -> None:
        if self.admin_token is None:
            raise PermissionError("admin mode is disabled on this server")
        token = request.get("admin_token")
        if not isinstance(token, str) or token != self.admin_token:
            raise PermissionError("invalid admin token")

    def _create_game(self, request: dict[str, object]) -> None:
        if self._sessions:
            raise ValueError("admin game creation requires no connected players")
        if self.game.phase.value not in {"waiting", "finished", "ready"}:
            raise ValueError("admin game creation is unavailable during an active hand")
        small_blind = self._request_integer(request, "small_blind", minimum=1)
        big_blind = self._request_integer(request, "big_blind", minimum=small_blind)
        minimum_players = self._request_integer(request, "minimum_players", minimum=2)
        maximum_players = self._request_integer(request, "maximum_players", minimum=minimum_players)
        auto_start_delay = self._request_number(request, "auto_start_delay", minimum=0.1)
        minimum_buy_in = self._request_integer(request, "minimum_buy_in", minimum=1)
        maximum_buy_in = self._request_integer(request, "maximum_buy_in", minimum=minimum_buy_in)
        seed = request.get("seed")
        if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
            raise ValueError("seed must be an integer or null")
        game = PokerGame.texas_holdem_no_limit(
            small_blind=small_blind,
            big_blind=big_blind,
            minimum_players=minimum_players,
            maximum_players=maximum_players,
            seed=seed,
        )
        self.minimum_players = minimum_players
        self.maximum_players = maximum_players
        self.auto_start_delay = auto_start_delay
        self.minimum_buy_in = minimum_buy_in
        self.maximum_buy_in = maximum_buy_in
        self._seed = seed
        self.game = game
        self._auto_start_deadline = None

    @staticmethod
    def _request_integer(request: dict[str, object], name: str, *, minimum: int) -> int:
        value = request.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"{name} must be an integer >= {minimum}")
        return value

    @staticmethod
    def _request_number(request: dict[str, object], name: str, *, minimum: float) -> float:
        value = request.get(name)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < minimum
        ):
            raise ValueError(f"{name} must be a number >= {minimum}")
        return float(value)

    def detach(self, identity: str | None, connection_id: int) -> None:
        """Mark a disconnected connection unavailable for interactive turns."""
        if identity is None:
            return
        with self._lock:
            session = self._sessions.get(identity)
            if session is not None and session.connection_id == connection_id:
                self._sessions.pop(identity, None)
                self.activity.append(f"DISCONNECTED {identity}; native check/fold fallback")
                self._reset_auto_start()

    def service_actions(self) -> None:
        """Run the server-owned timeout policy from the TCP server loop."""
        with self._lock:
            phase = self.game.phase.value
            if phase == "ready":
                if len(self._sessions) < self.minimum_players:
                    self._auto_start_deadline = None
                elif self._auto_start_deadline is None:
                    self._auto_start_deadline = time.monotonic() + self.auto_start_delay
                elif time.monotonic() >= self._auto_start_deadline:
                    self.game.start_hand()
                    self.activity.append("HAND started by lobby timer")
                    self._auto_start_deadline = None
                return
            self._auto_start_deadline = None
            current = self.game.current_player
            if current is None:
                return
            turn = (current, self.game.state_version)
            if turn != self._turn:
                self._turn = turn
                self._turn_started = time.monotonic()
            session = self._sessions.get(current)
            if session is not None and time.monotonic() - self._turn_started < self.action_timeout:
                return
            legal = self.game.legal_actions
            if legal is None:
                return
            if legal.can_check:
                self.game.check()
                self.activity.append(f"TIMEOUT/AWAY {current}: check")
                return
            if legal.can_fold:
                self.game.fold()
                self.activity.append(f"TIMEOUT/AWAY {current}: fold")

    def _touch(self, identity: str, connection_id: int) -> None:
        session = self._sessions.get(identity)
        if session is None or session.connection_id != connection_id:
            raise PermissionError("connection is not bound to this player")
        session.last_activity = time.monotonic()

    def _public_view(self) -> dict[str, object]:
        state = self._serialize_view(self.game.public_view())
        state["auto_start_seconds"] = self._auto_start_seconds()
        return state

    def _player_view(self, player_id: str) -> dict[str, object]:
        state = self._serialize_view(self.game.player_view(player_id))
        state["auto_start_seconds"] = self._auto_start_seconds()
        return state

    def _reset_auto_start(self) -> None:
        if self.game.phase.value == "ready" and len(self._sessions) >= self.minimum_players:
            self._auto_start_deadline = time.monotonic() + self.auto_start_delay
        else:
            self._auto_start_deadline = None

    def _auto_start_seconds(self) -> int | None:
        if self._auto_start_deadline is None or self.game.phase.value != "ready":
            return None
        return max(0, math.ceil(self._auto_start_deadline - time.monotonic()))

    def _serialize_view(self, view: GameView) -> dict[str, object]:
        occupied = {player.seat for player in view.players}
        return {
            "version": view.state_version,
            "phase": view.phase.value,
            "hand_id": view.hand_id,
            "dealer_seat": view.dealer_seat,
            "street": view.street.name if view.street is not None else None,
            "current_player": view.current_player,
            "connected_players": sorted(self._sessions),
            "pot": view.pot,
            "buy_in": {"minimum": self.minimum_buy_in, "maximum": self.maximum_buy_in},
            "available_seats": [
                seat for seat in range(self.maximum_players) if seat not in occupied
            ],
            "board": [str(card) for card in view.board],
            "players": [
                {
                    "player_id": player.player_id,
                    "seat": player.seat,
                    "connected": player.player_id in self._sessions,
                    "stack": player.stack,
                    "street_committed": player.street_committed,
                    "total_contributed": player.total_contributed,
                    "status": player.status.value if player.status is not None else None,
                    "cards": (
                        None if player.cards is None else [str(card) for card in player.cards]
                    ),
                }
                for player in view.players
            ],
        }

    @staticmethod
    def _range(value: AmountRange | None) -> dict[str, int] | None:
        return None if value is None else {"minimum": value.minimum, "maximum": value.maximum}

    def _action(self, request: dict[str, object]) -> Action:
        name = request.get("action")
        if not isinstance(name, str):
            raise ValueError("request field 'action' must be a string")
        if name == "all_in":
            legal = self.game.legal_actions
            if legal is not None and legal.raise_to is not None:
                return Action.raise_to(legal.raise_to.maximum)
            if legal is not None and legal.bet_to is not None:
                return Action.bet_to(legal.bet_to.maximum)
            raise ValueError("all_in is unavailable for the current action")
        if name in (ActionType.BET_TO.value, ActionType.RAISE_TO.value):
            total = self._integer(request, "total", minimum=0)
            return (
                Action.bet_to(total) if name == ActionType.BET_TO.value else Action.raise_to(total)
            )
        if name == ActionType.FOLD.value:
            return Action.fold()
        if name == ActionType.CHECK.value:
            return Action.check()
        if name == ActionType.CALL.value:
            return Action.call()
        raise ValueError(f"unknown action: {name}")

    @staticmethod
    def _ack(identity: str, *, resumed: bool) -> dict[str, object]:
        return {"ok": True, "identity": identity, "resumed": resumed, "events": []}

    @staticmethod
    def _record(record: CommandRecord, identity: str | None) -> dict[str, object]:
        return {
            "ok": True,
            "identity": identity,
            "cursor": record.event_cursor_after,
            "version": record.version_after,
            "events": [type(event).__name__ for event in record.events],
        }

    @staticmethod
    def _require_identity(identity: str | None) -> str:
        if identity is None:
            raise PermissionError("join first to control a player")
        return identity

    @staticmethod
    def _string(request: dict[str, object], name: str) -> str:
        value = request.get(name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"request field '{name}' must be a non-empty string")
        return value

    def _buy_in(self, request: dict[str, object]) -> int:
        value = self._integer(request, "stack")
        if not self.minimum_buy_in <= value <= self.maximum_buy_in:
            raise ValueError(
                f"stack must be between {self.minimum_buy_in} and {self.maximum_buy_in}"
            )
        return value

    @staticmethod
    def _integer(request: dict[str, object], name: str, *, minimum: int | None = None) -> int:
        value = request.get(name)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"request field '{name}' must be an integer")
        if minimum is not None and value < minimum:
            raise ValueError(f"request field '{name}' must be >= {minimum}")
        return value


class ClientHandler(socketserver.StreamRequestHandler):
    """Handle one newline-delimited JSON client connection."""

    MAX_LINE = 64 * 1024

    @property
    def _application(self) -> PokerApplication:
        server = self.server
        if not isinstance(server, PokerTCPServer):
            raise RuntimeError("client handler is attached to an unexpected server type")
        return server.application

    def handle(self) -> None:
        identity: str | None = None
        connection_id = id(self)
        self._send({"ok": True, "server": "pokerengine", "tls": False})
        try:
            while True:
                try:
                    raw = self.rfile.readline(self.MAX_LINE + 1)
                except (
                    ConnectionResetError,
                    ConnectionAbortedError,
                    BrokenPipeError,
                    TimeoutError,
                    OSError,
                ):
                    # A client closing its terminal/socket is a normal disconnect.
                    return
                if not raw:
                    return
                if len(raw) > self.MAX_LINE:
                    self._send({"ok": False, "error": "request is too large"})
                    return
                try:
                    decoded: object = json.loads(raw)
                    request = _json_object(decoded)
                    response, identity = self._application.handle(request, identity, connection_id)
                except MemoryError:
                    raise
                except Exception as error:  # keep protocol errors local to this request
                    self._application.note_rejection(identity, type(error).__name__)
                    self._send({"ok": False, "error": str(error), "type": type(error).__name__})
                    continue
                self._send(response)
        finally:
            self._application.detach(identity, connection_id)

    def _send(self, response: dict[str, object]) -> None:
        try:
            self.wfile.write((json.dumps(response, separators=(",", ":")) + "\n").encode())
            self.wfile.flush()
        except (
            ConnectionResetError,
            ConnectionAbortedError,
            BrokenPipeError,
            TimeoutError,
            OSError,
        ):
            # The peer may leave between request handling and response delivery.
            return


class PokerTCPServer(socketserver.ThreadingTCPServer):
    """Thread-per-connection TCP host for the example application."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], application: PokerApplication) -> None:
        super().__init__(address, ClientHandler)
        self.application = application

    def service_actions(self) -> None:
        self.application.service_actions()
        super().service_actions()


def _prompt_text(label: str, default: str) -> str:
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def _prompt_int(label: str, default: int, *, minimum: int = 0) -> int:
    while True:
        raw = input(f"{label} [{default}]: ").strip()
        try:
            value = int(raw or default)
        except ValueError:
            _write_line("Enter a whole number.")
            continue
        if value >= minimum:
            return value
        _write_line(f"Value must be at least {minimum}.")


def _prompt_float(label: str, default: float, *, minimum: float = 0.1) -> float:
    while True:
        raw = input(f"{label} [{default:g}]: ").strip()
        try:
            value = float(raw or default)
        except ValueError:
            _write_line("Enter a number.")
            continue
        if value >= minimum:
            return value
        _write_line(f"Value must be at least {minimum:g}.")


def _prompt_server_config_line() -> tuple[str, int, ServerConfig, bool]:
    _write_line(
        "\n╔══════════════════════════════════════╗\n"
        "║        PokerEngine Server Setup       ║\n"
        "╚══════════════════════════════════════╝"
    )
    host = _prompt_text("Host", "127.0.0.1")
    port = _prompt_int("Port", 8765, minimum=1)
    _write_line("\n[Table rules]")
    small_blind = _prompt_int("Small blind", 50, minimum=1)
    big_blind = _prompt_int("Big blind", 100, minimum=small_blind)
    minimum_players = _prompt_int("Minimum players", 2, minimum=2)
    maximum_players = _prompt_int("Maximum players", 10, minimum=minimum_players)
    _write_line("\n[Lobby policy]")
    auto_start_delay = _prompt_float("Auto-start delay seconds", 15.0)
    minimum_buy_in = _prompt_int("Minimum buy-in", 100, minimum=1)
    maximum_buy_in = _prompt_int("Maximum buy-in", 10_000, minimum=minimum_buy_in)
    action_timeout = _prompt_float("Action timeout seconds", 8.0)
    _write_line("\n[Security]")
    admin_token = input("Admin token (blank disables admin mode): ").strip() or None
    allow_private = input("Enable local private-card mode? [y/N]: ").strip().lower() in {
        "y",
        "yes",
    }
    if allow_private and not ipaddress.ip_address(host).is_loopback:
        _write_line(
            f"\nWARNING: private cards will cross an unauthenticated, non-TLS network on {host}."
        )
        confirmation = input(
            "Type ENABLE PRIVATE CARDS to continue, or press Enter to disable: "
        ).strip()
        if confirmation != "ENABLE PRIVATE CARDS":
            _write_line("Continuing in public-only mode.")
            allow_private = False
    return (
        host,
        port,
        ServerConfig(
            small_blind=small_blind,
            big_blind=big_blind,
            minimum_players=minimum_players,
            maximum_players=maximum_players,
            auto_start_delay=auto_start_delay,
            action_timeout=action_timeout,
            minimum_buy_in=minimum_buy_in,
            maximum_buy_in=maximum_buy_in,
            admin_token=admin_token,
        ),
        allow_private,
    )


class ServerSetupUI:
    """Full-screen setup wizard matching the terminal client visual language."""

    def run(self, screen: curses.window) -> tuple[str, int, ServerConfig, bool]:
        self.screen = screen
        self.section = "CONNECTION"
        self.step = 0
        curses.echo()
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_CYAN, -1)
            curses.init_pair(2, curses.COLOR_YELLOW, -1)
            curses.init_pair(3, curses.COLOR_GREEN, -1)
        try:
            host = self._text("Network host", "127.0.0.1")
            port = self._integer("TCP port", 8765, 1)
            self._section("TABLE RULES")
            small_blind = self._integer("Small blind", 50, 1)
            big_blind = self._integer("Big blind", 100, small_blind)
            minimum_players = self._integer("Minimum players", 2, 2)
            maximum_players = self._integer("Maximum players", 10, minimum_players)
            self._section("LOBBY POLICY")
            auto_start_delay = self._number("Auto-start seconds", 15.0)
            minimum_buy_in = self._integer("Minimum buy-in", 100, 1)
            maximum_buy_in = self._integer("Maximum buy-in", 10_000, minimum_buy_in)
            action_timeout = self._number("Action timeout seconds", 8.0)
            self._section("SECURITY")
            admin_token = self._text("Admin token (blank disables)", "") or None
            private_requested = self._text("Private cards? (y/N)", "N").lower() in {"y", "yes"}
            allow_private = private_requested
            if private_requested and not ipaddress.ip_address(host).is_loopback:
                self._warning(f"Private cards on {host} expose secrets over unauthenticated TCP.")
                allow_private = (
                    self._text("Type ENABLE PRIVATE CARDS to confirm", "") == "ENABLE PRIVATE CARDS"
                )
            self._summary(
                host,
                port,
                small_blind,
                big_blind,
                minimum_players,
                maximum_players,
                auto_start_delay,
                minimum_buy_in,
                maximum_buy_in,
                action_timeout,
                bool(admin_token),
                allow_private,
            )
            return (
                host,
                port,
                ServerConfig(
                    small_blind=small_blind,
                    big_blind=big_blind,
                    minimum_players=minimum_players,
                    maximum_players=maximum_players,
                    auto_start_delay=auto_start_delay,
                    action_timeout=action_timeout,
                    minimum_buy_in=minimum_buy_in,
                    maximum_buy_in=maximum_buy_in,
                    admin_token=admin_token,
                ),
                allow_private,
            )
        finally:
            curses.noecho()

    def _text(self, label: str, default: str) -> str:
        self.step += 1
        value = ""
        curses.noecho()
        self.screen.timeout(-1)
        while True:
            self._render(label, default, value)
            key = self.screen.getch()
            if key in (curses.KEY_ENTER, 10, 13):
                return value or default
            if key in (curses.KEY_BACKSPACE, 127, 8):
                value = value[:-1]
            elif 32 <= key <= 126 and len(value) < 64:
                value += chr(key)

    def _integer(self, label: str, default: int, minimum: int) -> int:
        while True:
            value = self._text(label, str(default))
            try:
                parsed = int(value)
            except ValueError:
                self._message("Enter a whole number.", 2)
                continue
            if parsed >= minimum:
                return parsed
            self._message(f"Value must be at least {minimum}.", 2)

    def _number(self, label: str, default: float) -> float:
        while True:
            value = self._text(label, f"{default:g}")
            try:
                parsed = float(value)
            except ValueError:
                self._message("Enter a number.", 2)
                continue
            if parsed >= 0.1:
                return parsed
            self._message("Value must be at least 0.1.", 2)

    def _section(self, title: str) -> None:
        self.section = title
        self.step = 0

    def _warning(self, message: str) -> None:
        self._message(f"⚠ {message}", 2, curses.color_pair(2) | curses.A_BOLD)

    def _render(self, label: str, default: str, value: str = "") -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        title = "♠ ♥ ♦  POKERENGINE SERVER SETUP  ♣ ♦ ♥ ♠"
        center = max(0, (width - len(title)) // 2)
        self.screen.addnstr(
            1, center, title, max(1, width - 2), curses.color_pair(1) | curses.A_BOLD
        )
        section = f"{self.section}  ·  STEP {self.step}"
        self.screen.addnstr(
            3,
            max(0, (width - len(section)) // 2),
            section,
            max(1, width - 2),
            curses.color_pair(2) | curses.A_BOLD,
        )
        box_width = min(72, max(40, width - 8))
        left = max(2, (width - box_width) // 2)
        top = height // 2 - 4
        self.screen.addnstr(
            top, left, "┌" + "─" * (box_width - 2) + "┐", box_width, curses.color_pair(1)
        )
        self.screen.addnstr(top + 1, left, f"│{label:^{box_width - 2}}│", box_width, curses.A_BOLD)
        self.screen.addnstr(top + 2, left, "│" + " " * (box_width - 2) + "│", box_width)
        shown = value if value else (f"{default}  (placeholder)" if default else "Type a value")
        value_attr = curses.A_NORMAL if value else curses.A_DIM
        self.screen.addnstr(top + 3, left, f"│{shown:^{box_width - 2}}│", box_width, value_attr)
        self.screen.addnstr(top + 4, left, "│" + " " * (box_width - 2) + "│", box_width)
        self.screen.addnstr(
            top + 5, left, "└" + "─" * (box_width - 2) + "┘", box_width, curses.color_pair(1)
        )
        self.screen.addnstr(
            top + 7,
            max(0, (width - 38) // 2),
            "Type to replace · Backspace clears · ENTER keeps default",
            max(1, width - 2),
            curses.color_pair(3),
        )
        self.screen.move(top + 4, left + 3)
        self.screen.refresh()

    def _summary(
        self,
        host: str,
        port: int,
        small_blind: int,
        big_blind: int,
        minimum_players: int,
        maximum_players: int,
        auto_start_delay: float,
        minimum_buy_in: int,
        maximum_buy_in: int,
        action_timeout: float,
        admin_enabled: bool,
        private_enabled: bool,
    ) -> None:
        lines = [
            "Configuration summary",
            f"Network       {host}:{port}",
            f"Blinds        {small_blind}/{big_blind}",
            f"Players       {minimum_players}-{maximum_players}",
            f"Auto-start    {auto_start_delay:g}s",
            f"Buy-in        {minimum_buy_in}-{maximum_buy_in}",
            f"Action timeout {action_timeout:g}s",
            f"Admin token   {'enabled' if admin_enabled else 'disabled'}",
            f"Private cards {'enabled' if private_enabled else 'public-only'}",
        ]
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        top = max(2, (height - len(lines)) // 2)
        for offset, line in enumerate(lines):
            attr = curses.color_pair(1) | curses.A_BOLD if offset == 0 else 0
            self.screen.addnstr(
                top + offset, max(0, (width - len(line)) // 2), line, max(1, width - 2), attr
            )
        prompt = "Press ENTER to start server · Q to cancel"
        self.screen.addnstr(
            height - 2,
            max(0, (width - len(prompt)) // 2),
            prompt,
            max(1, width - 2),
            curses.color_pair(3),
        )
        self.screen.refresh()
        while self.screen.getch() not in (curses.KEY_ENTER, 10, 13, ord("q"), ord("Q")):
            pass

    def _message(self, message: str, row: int, attr: int = 0) -> None:
        self.screen.erase()
        _, width = self.screen.getmaxyx()
        self.screen.addnstr(
            row + 2, max(0, (width - len(message)) // 2), message, max(1, width - 2), attr
        )
        self.screen.refresh()
        if row != 0:
            self.screen.getch()


class ServerDashboard:
    """Responsive public monitor: no trusted view or private cards are read."""

    def __init__(self, application: PokerApplication, host: str, port: int) -> None:
        self.application = application
        self.host = host
        self.port = port
        self.last_hand_id: int | None = None

    def run(self, screen: curses.window) -> None:
        screen.timeout(200)
        with suppress(curses.error):
            curses.curs_set(0)
        while True:
            self._draw(screen)
            if screen.getch() in (ord("q"), ord("Q")):
                return

    def _draw(self, screen: curses.window) -> None:
        screen.erase()
        height, width = screen.getmaxyx()
        view, connected, activity, result = self.application.dashboard_snapshot()
        if view.hand_id is not None:
            self.last_hand_id = view.hand_id
        hand_label = self.last_hand_id if self.last_hand_id is not None else "-"
        lines = [
            f"POKERENGINE / PUBLIC SERVER   {self.host}:{self.port}   "
            f"CLIENTS {len(connected)}   [Q] stop",
            f"HAND {hand_label} | {view.phase.value.upper()} / "
            f"{view.street.name if view.street else 'LOBBY'}"
            f" | TURN {view.current_player or '-'} | POT {view.pot}",
            "BOARD "
            + ("  ".join(str(card) for card in view.board) or "--")
            + "   (s=spades h=hearts d=diamonds c=clubs)",
            "SEAT  PLAYER           CONNECTION  STACK  BET  STATUS",
        ]
        for player in view.players:
            lines.append(
                f"{player.seat:>4}  {player.player_id:<16} "
                f"{'online' if player.player_id in connected else 'away':<10} "
                f"{player.stack:>5} {player.street_committed:>4}  "
                f"{player.status.value if player.status else 'waiting'}"
            )
        if result:
            lines.append(result)
        lines.append("RECENT ACTIONS / REJECTIONS / DISCONNECTS")
        lines.extend(activity[-max(1, height - len(lines) - 1) :])
        for row, line in enumerate(lines[:height]):
            with suppress(curses.error):
                screen.addnstr(row, 1, line, max(1, width - 2), curses.A_BOLD if row == 0 else 0)
        screen.refresh()


def _prompt_server_config() -> tuple[str, int, ServerConfig, bool]:
    if sys.stdin.isatty() and sys.stdout.isatty():
        return curses.wrapper(ServerSetupUI().run)
    return _prompt_server_config_line()


def run_server(
    host: str,
    port: int,
    config: ServerConfig,
    *,
    allow_private: bool,
    dashboard: bool,
) -> None:
    """Own socket/thread lifetime for interactive and headless entrypoints."""
    application = PokerApplication(config, allow_private=allow_private)
    server = PokerTCPServer((host, port), application)
    if dashboard:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            curses.wrapper(ServerDashboard(application, host, port).run)
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
        return
    try:
        _write_line(
            f"PokerEngine TCP server listening on {host}:{port} "
            f"(TLS/auth disabled; local demonstration)"
        )
        server.serve_forever()
    except KeyboardInterrupt:
        _write_line("\nServer stopped.")
    finally:
        server.server_close()


app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)


@app.command()
def serve(
    headless: Annotated[
        bool, typer.Option(help="Use explicit options without setup/dashboard.")
    ] = False,
    dashboard: Annotated[
        bool, typer.Option(help="Use explicit options with the public dashboard.")
    ] = False,
    host: Annotated[str, typer.Option(help="Loopback by default; no TLS/auth.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8765,
    seed: Annotated[int | None, typer.Option(help="Synthetic deterministic deck seed.")] = None,
    private: Annotated[
        bool, typer.Option(help="Enable connection-bound player views on loopback.")
    ] = False,
    auto_start_delay: Annotated[float, typer.Option(min=0.1)] = 60,
    action_timeout: Annotated[float, typer.Option(min=0.1)] = 60,
) -> None:
    """Run the existing native-engine TCP example; no production authentication."""
    if not headless and not dashboard:
        host, port, config, private = _prompt_server_config()
    else:
        if not math.isfinite(auto_start_delay) or not math.isfinite(action_timeout):
            raise typer.BadParameter("Timeouts must be finite.")
        if private and not ipaddress.ip_address(host).is_loopback:
            raise typer.BadParameter("Private views require a loopback address.")
        config = ServerConfig(
            seed=seed, auto_start_delay=auto_start_delay, action_timeout=action_timeout
        )
    if not ipaddress.ip_address(host).is_loopback:
        raise typer.BadParameter("This test-only example requires a loopback address.")
    run_server(
        host,
        port,
        config,
        allow_private=private,
        dashboard=(dashboard or not headless) and sys.stdin.isatty() and sys.stdout.isatty(),
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
