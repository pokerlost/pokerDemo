#!/usr/bin/env python3
"""Curses client for the public PokerEngine newline-delimited TCP protocol.

The current server intentionally exposes only public table state. This client
therefore never assumes that a missing card list is an error or a permission
grant; private cards require the explicit loopback private mode.
"""

from __future__ import annotations

import curses
import json
import socket
import sys
import textwrap
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Annotated

import typer

SUITS = {"s": "♠", "h": "♥", "d": "♦", "c": "♣"}
ACTION_LABELS = {
    "fold": "Fold",
    "check": "Check",
    "call": "Call",
    "bet_to": "Bet To",
    "raise_to": "Raise To",
    "all_in": "ALL IN",
}


def action_label(name: str) -> str:
    """Return a display label for one wire action discriminator."""
    return ACTION_LABELS.get(name, name.replace("_", " ").title())


class ServerError(RuntimeError):
    """A rejected request or malformed server response."""


def _wire_object(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ServerError(path + " must be a JSON object")
    result: dict[str, object] = {}
    for key, item in value.items():
        if type(key) is not str:
            raise ServerError(path + " contains a non-string object key")
        result[key] = item
    return result


def _wire_array(value: object, path: str) -> list[object]:
    if type(value) is not list:
        raise ServerError(path + " must be a JSON array")
    result: list[object] = []
    result.extend(value)
    return result


def _wire_int(value: object, path: str) -> int:
    if type(value) is not int:
        raise ServerError(path + " must be an integer")
    return value


def _wire_optional_int(value: object, path: str) -> int | None:
    return None if value is None else _wire_int(value, path)


def _wire_str(value: object, path: str) -> str:
    if type(value) is not str:
        raise ServerError(path + " must be a string")
    return value


def _wire_optional_str(value: object, path: str) -> str | None:
    return None if value is None else _wire_str(value, path)


def _wire_strings(value: object, path: str) -> tuple[str, ...]:
    return tuple(
        _wire_str(item, f"{path}[{index}]") for index, item in enumerate(_wire_array(value, path))
    )


class TCPTransport:
    """Adapter around one newline-delimited JSON/TCP connection."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        last_error: OSError | None = None
        for _ in range(12):
            try:
                self._socket = socket.create_connection((host, port), timeout=2)
                # The connect timeout must not leak into blocking request reads.
                self._socket.settimeout(5)
                break
            except OSError as error:
                last_error = error
                time.sleep(0.5)
        else:
            raise ServerError(
                f"cannot connect to PokerEngine server at {host}:{port}; "
                "start the server first and keep its dashboard open"
            ) from last_error
        self._reader = self._socket.makefile("rb")
        self._writer = self._socket.makefile("wb")
        try:
            hello = self._read()
            if hello.get("server") != "pokerengine":
                raise ServerError("unexpected server greeting")
        except BaseException:
            self.close()
            raise

    def request(self, payload: dict[str, object]) -> dict[str, object]:
        try:
            self._writer.write((json.dumps(payload, separators=(",", ":")) + "\n").encode())
            self._writer.flush()
        except OSError as error:
            raise ServerError("connection to PokerEngine server was lost") from error
        return self._read()

    def close(self) -> None:
        for resource in (self._reader, self._writer, self._socket):
            with suppress(OSError):
                resource.close()

    def _read(self) -> dict[str, object]:
        try:
            raw = self._reader.readline()
        except (
            ConnectionResetError,
            ConnectionAbortedError,
            BrokenPipeError,
            TimeoutError,
            OSError,
        ) as error:
            raise ServerError("connection to PokerEngine server was lost") from error
        if not raw:
            raise ServerError("server closed the connection")
        try:
            decoded: object = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ServerError("server sent invalid JSON") from error
        response = _wire_object(decoded, "response")
        if response.get("ok") is not True:
            message = response.get("error")
            if type(message) is not str:
                raise ServerError("server rejected the request without an error message")
            raise ServerError(message)
        return response


@dataclass(frozen=True, slots=True)
class PlayerState:
    """Validated client-side projection of one player from the wire."""

    player_id: str
    seat: int
    stack: int
    street_committed: int
    total_contributed: int
    status: str | None
    cards: tuple[str, ...] | None
    connected: bool = True


@dataclass(frozen=True, slots=True)
class TableState:
    """Validated client-side projection of the current table state."""

    version: int
    phase: str
    hand_id: int | None
    available_seats: tuple[int, ...]
    minimum_buy_in: int
    maximum_buy_in: int
    auto_start_seconds: int | None
    dealer_seat: int | None
    street: str | None
    current_player: str | None
    pot: int
    board: tuple[str, ...]
    players: tuple[PlayerState, ...]
    connected_players: tuple[str, ...] = ()

    @staticmethod
    def _player_from_wire(raw: object, index: int) -> PlayerState:
        path = f"state.players[{index}]"
        player = _wire_object(raw, path)
        cards_raw = player.get("cards")
        return PlayerState(
            player_id=_wire_str(player.get("player_id"), path + ".player_id"),
            seat=_wire_int(player.get("seat"), path + ".seat"),
            stack=_wire_int(player.get("stack"), path + ".stack"),
            street_committed=_wire_int(player.get("street_committed"), path + ".street_committed"),
            total_contributed=_wire_int(
                player.get("total_contributed"), path + ".total_contributed"
            ),
            status=_wire_optional_str(player.get("status"), path + ".status"),
            cards=None if cards_raw is None else _wire_strings(cards_raw, path + ".cards"),
            connected=player.get("connected", True) is True,
        )

    @classmethod
    def from_dict(cls, raw: object) -> TableState:
        data = _wire_object(raw, "state")
        buy_in = _wire_object(data.get("buy_in"), "state.buy_in")
        available_seats = tuple(
            _wire_int(seat, f"state.available_seats[{index}]")
            for index, seat in enumerate(
                _wire_array(data.get("available_seats"), "state.available_seats")
            )
        )
        players = tuple(
            cls._player_from_wire(player, index)
            for index, player in enumerate(_wire_array(data.get("players"), "state.players"))
        )
        return cls(
            version=_wire_int(data.get("version"), "state.version"),
            phase=_wire_str(data.get("phase"), "state.phase"),
            hand_id=_wire_optional_int(data.get("hand_id"), "state.hand_id"),
            available_seats=available_seats,
            minimum_buy_in=_wire_int(buy_in.get("minimum"), "state.buy_in.minimum"),
            maximum_buy_in=_wire_int(buy_in.get("maximum"), "state.buy_in.maximum"),
            auto_start_seconds=_wire_optional_int(
                data.get("auto_start_seconds"), "state.auto_start_seconds"
            ),
            dealer_seat=_wire_optional_int(data.get("dealer_seat"), "state.dealer_seat"),
            street=_wire_optional_str(data.get("street"), "state.street"),
            current_player=_wire_optional_str(data.get("current_player"), "state.current_player"),
            pot=_wire_int(data.get("pot"), "state.pot"),
            board=_wire_strings(data.get("board"), "state.board"),
            players=players,
            connected_players=_wire_strings(
                data.get("connected_players", []), "state.connected_players"
            ),
        )


@dataclass(frozen=True, slots=True)
class LegalState:
    """Validated client-side projection of authoritative legal actions."""

    types: tuple[str, ...]
    call: int | None
    bet_to: tuple[int, int] | None
    raise_to: tuple[int, int] | None

    @staticmethod
    def _amount_range(value: object, path: str) -> tuple[int, int] | None:
        if value is None:
            return None
        data = _wire_object(value, path)
        return (
            _wire_int(data.get("minimum"), path + ".minimum"),
            _wire_int(data.get("maximum"), path + ".maximum"),
        )

    @classmethod
    def from_dict(cls, raw: object) -> LegalState | None:
        if raw is None:
            return None
        data = _wire_object(raw, "legal_actions")
        types = _wire_strings(data.get("types"), "legal_actions.types")
        bet_to = cls._amount_range(data.get("bet_to"), "legal_actions.bet_to")
        raise_to = cls._amount_range(data.get("raise_to"), "legal_actions.raise_to")
        if bet_to is not None or raise_to is not None:
            types += ("all_in",)
        return cls(
            types=types,
            call=_wire_optional_int(data.get("call"), "legal_actions.call"),
            bet_to=bet_to,
            raise_to=raise_to,
        )


class NicknamePrompt:
    """Collect a non-empty player nickname before joining the table."""

    def read(self, screen: curses.window) -> str:
        while True:
            raw = self._read_centered(screen, "WELCOME TO POKERENGINE", "Nickname", 32)
            nickname = raw.decode("utf-8", errors="ignore").strip()
            if nickname:
                return nickname

    def read_buy_in(self, screen: curses.window, minimum: int, maximum: int) -> int:
        while True:
            raw = self._read_centered(
                screen, "CHOOSE YOUR BUY-IN", f"Stack ({minimum:,}-{maximum:,})", 16
            )
            try:
                value = int(raw.decode("ascii", errors="ignore").strip())
            except ValueError:
                continue
            if minimum <= value <= maximum:
                return value

    @staticmethod
    def _read_centered(screen: curses.window, title: str, label: str, max_length: int) -> bytes:
        screen.timeout(-1)
        curses.echo()
        try:
            screen.erase()
            _height, width = screen.getmaxyx()
            center = width // 2
            title_column = max(0, center - len(title) // 2)
            label_column = max(0, center - len(label) // 2)
            box_width = min(max_length + 4, max(20, width - 8))
            box_left = max(1, center - box_width // 2)
            screen.addnstr(1, title_column, title, max(1, width - title_column - 1), curses.A_BOLD)
            screen.addnstr(3, label_column, label, max(1, width - label_column - 1), 0)
            screen.addnstr(
                5, box_left, "┌" + "─" * (box_width - 2) + "┐", max(1, width - box_left - 1)
            )
            screen.addnstr(
                6, box_left, "│" + " " * (box_width - 2) + "│", max(1, width - box_left - 1)
            )
            screen.addnstr(
                7, box_left, "└" + "─" * (box_width - 2) + "┘", max(1, width - box_left - 1)
            )
            screen.addnstr(
                9,
                max(0, center - 21),
                "Type your value · ENTER confirms",
                max(1, width - max(0, center - 21) - 1),
                curses.A_DIM,
            )
            screen.refresh()
            return screen.getstr(6, box_left + 2, max(1, box_width - 4))
        finally:
            curses.noecho()

    @staticmethod
    def _line(
        screen: curses.window, row: int, column: int, text: str, attr: int, width: int
    ) -> None:
        if 0 <= row < screen.getmaxyx()[0]:
            with suppress(curses.error):
                screen.addnstr(row, column, text, max(1, width - column - 1), attr)


class SeatPicker:
    """Interactive seat-selection screen shown before the player joins."""

    def __init__(self) -> None:
        self.notice = ""

    def pick(self, screen: curses.window, seats: tuple[int, ...]) -> int | None:
        if not seats:
            self.notice = "No seats are currently available."
            return None
        index = 0
        screen.timeout(-1)
        while True:
            screen.erase()
            height, width = screen.getmaxyx()
            self._center(screen, 1, "CHOOSE YOUR SEAT", width, curses.A_BOLD)
            self._center(screen, 3, "←/→ or ↑/↓ move · ENTER sit down · Q cancel", width, 0)
            if self.notice:
                self._center(screen, 5, self.notice, width, curses.A_BOLD)
                self.notice = ""
            labels = "   ".join(
                f"{'▶' if position == index else ' '} Seat {seat}"
                for position, seat in enumerate(seats)
            )
            self._center(screen, height // 2, labels, width, curses.A_BOLD)
            self._center(
                screen,
                height // 2 + 2,
                f"Available seats: {', '.join(map(str, seats))}",
                width,
                0,
            )
            screen.refresh()
            key = screen.getch()
            if key in (ord("q"), ord("Q")):
                return None
            if key in (curses.KEY_LEFT, curses.KEY_UP):
                index = (index - 1) % len(seats)
            elif key in (curses.KEY_RIGHT, curses.KEY_DOWN):
                index = (index + 1) % len(seats)
            elif key in (curses.KEY_ENTER, 10, 13):
                return seats[index]

    @staticmethod
    def _center(screen: curses.window, row: int, text: str, width: int, attr: int) -> None:
        SeatPicker._line(screen, row, max(1, (width - len(text)) // 2), text, attr, width)

    @staticmethod
    def _line(
        screen: curses.window, row: int, column: int, text: str, attr: int, width: int
    ) -> None:
        if 0 <= row < screen.getmaxyx()[0]:
            with suppress(curses.error):
                screen.addnstr(row, column, text, max(1, width - column - 1), attr)


class PokerProtocol:
    """Facade translating typed client operations to wire messages."""

    def __init__(self, transport: TCPTransport) -> None:
        self.transport = transport

    def join(self, player_id: str, seat: int, stack: int) -> dict[str, object]:
        return self.transport.request(
            {"op": "join", "player_id": player_id, "seat": seat, "stack": stack}
        )

    def admin_create_game(self, token: str, config: dict[str, object]) -> dict[str, object]:
        return self.transport.request({"op": "admin_create_game", "admin_token": token, **config})

    def start(self) -> dict[str, object]:
        return self.transport.request({"op": "start"})

    def settle(self) -> dict[str, object]:
        return self.transport.request({"op": "settle"})

    def result(self) -> dict[str, object]:
        return self.transport.request({"op": "result"})

    def state(self) -> TableState:
        response = self.transport.request({"op": "state"})
        return TableState.from_dict(response.get("state"))

    def private_state(self) -> TableState:
        response = self.transport.request({"op": "private_state"})
        return TableState.from_dict(response.get("state"))

    def legal(self) -> LegalState | None:
        response = self.transport.request({"op": "legal"})
        return LegalState.from_dict(response.get("legal_actions"))

    def events(self, cursor: int) -> tuple[int, tuple[str, ...]]:
        response = self.transport.request({"op": "events", "cursor": cursor})
        return (
            _wire_int(response.get("cursor"), "response.cursor"),
            _wire_strings(response.get("events"), "response.events"),
        )

    def action(self, name: str, total: int | None = None) -> dict[str, object]:
        payload: dict[str, object] = {"op": "action", "action": name}
        if total is not None:
            payload["total"] = total
        return self.transport.request(payload)


class HistoryLog:
    """Append-only client activity log with a scrollable viewport."""

    def __init__(self) -> None:
        self.lines: list[str] = []
        self.offset = 0

    def add(self, message: str) -> None:
        self.lines.append(message)
        self.offset = max(0, len(self.lines) - 1)

    def scroll(self, delta: int, visible: int) -> None:
        maximum = max(0, len(self.lines) - visible)
        self.offset = max(0, min(maximum, self.offset + delta))

    def visible(self, count: int) -> list[str]:
        if not self.lines:
            return ["No client events yet."]
        return self.lines[self.offset : self.offset + count]


class CardRenderer:
    """Presentation strategy for compact cards and board rows."""

    @staticmethod
    def label(card: str) -> str:
        return f"{card[:-1]}{SUITS.get(card[-1].lower(), card[-1])}" if card else "??"

    @classmethod
    def row(cls, cards: tuple[str, ...]) -> list[str]:
        values = cards or ("--",)
        widgets = [["┌─────┐", f"│{cls.label(card):<5}│", "│     │", "└─────┘"] for card in values]
        return ["  ".join(row) for row in zip(*widgets, strict=True)]


class TerminalView:
    """Colorful client dashboard with a centered table and activity rail."""

    def __init__(self, history: HistoryLog, endpoint: str = "") -> None:
        self.history = history
        self.endpoint = endpoint
        self._color_pairs: dict[str, int] = {}

    def setup_colors(self, screen: curses.window) -> None:
        if not curses.has_colors():
            return
        curses.start_color()
        curses.use_default_colors()
        palette = (
            ("cyan", curses.COLOR_CYAN),
            ("blue", curses.COLOR_BLUE),
            ("green", curses.COLOR_GREEN),
            ("yellow", curses.COLOR_YELLOW),
            ("magenta", curses.COLOR_MAGENTA),
            ("red", curses.COLOR_RED),
            ("white", curses.COLOR_WHITE),
        )
        for name, color in palette:
            pair = len(self._color_pairs) + 1
            curses.init_pair(pair, color, -1)
            self._color_pairs[name] = curses.color_pair(pair)

    def color(self, name: str, fallback: int = 0) -> int:
        return self._color_pairs.get(name, fallback)

    def draw(
        self,
        screen: curses.window,
        state: TableState,
        player_id: str,
        legal: LegalState | None,
        *,
        command: str | None = None,
        result: str = "",
    ) -> None:
        """Fit the same live table into a tmux pane or a standalone terminal."""
        screen.erase()
        height, width = screen.getmaxyx()
        mine = next((player for player in state.players if player.player_id == player_id), None)
        cards = (
            " ".join(mine.cards) if mine is not None and mine.cards else "hidden / public capture"
        )
        lines = [
            f"POKERENGINE / {player_id}  {self.endpoint}",
            f"HAND {state.hand_id or '-'} | {state.phase.upper()} / "
            f"{state.street or 'LOBBY'} | POT {state.pot}",
            f"YOUR CARDS {cards}",
            "COMMUNITY BOARD " + ("  ".join(state.board) or "--"),
        ]
        for player in state.players:
            marker = ">" if player.player_id == state.current_player else " "
            lines.append(
                f"{marker} {player.player_id:<10} stack {player.stack:>5} "
                f"bet {player.street_committed:>4} "
                f"{player.status or 'waiting'}{' / away' if not player.connected else ''}"
            )
        if state.current_player == player_id and legal:
            choices = []
            for name in legal.types:
                if name == "call":
                    choices.append(f"call {legal.call}")
                elif name == "bet_to" and legal.bet_to:
                    choices.append(f"bet TOTAL [{legal.bet_to[0]}..{legal.bet_to[1]}]")
                elif name == "raise_to" and legal.raise_to:
                    choices.append(f"raise TOTAL [{legal.raise_to[0]}..{legal.raise_to[1]}]")
                elif name == "all_in":
                    bounds = legal.raise_to or legal.bet_to
                    choices.append(f"all_in {bounds[1]}" if bounds else name)
                else:
                    choices.append(name)
            lines.extend(textwrap.wrap("YOUR TURN: " + " | ".join(choices), max(10, width - 2)))
        else:
            lines.append(f"WAITING: {state.current_player or 'S start / D settle / N next hand'}")
        if result:
            lines.extend(textwrap.wrap(result, max(10, width - 2)))
        footer = [
            "F fold  C check  L call  A all-in  : command",
            "S/N start  D settle  Q quit  | bet/raise use :",
        ]
        history_room = max(0, height - len(lines) - len(footer) - 2)
        lines.extend(self.history.lines[-history_room:] if history_room else [])
        for row, line in enumerate(lines[: max(1, height - 3)]):
            self._line(screen, row, 1, line, curses.A_BOLD if row == 0 else 0, width)
        for row, line in enumerate(footer, max(0, height - 3)):
            self._line(screen, row, 1, line, self.color("cyan"), width)
        prompt = (
            ":" + command
            if command is not None
            else "Live table | card codes: As Kh Qd Jc | totals are street commitments"
        )
        self._line(screen, height - 1, 1, prompt, self.color("yellow"), width)
        screen.refresh()

    def _header(self, screen: curses.window, state: TableState, limit: int) -> None:
        title = "♠ ♥ ♦  POKERENGINE  ♦ ♥ ♠"
        self._center(screen, 0, title, limit, curses.A_BOLD | self.color("cyan"))
        countdown = (
            f" · AUTO START {state.auto_start_seconds}s"
            if state.auto_start_seconds is not None
            else ""
        )
        endpoint = f"  ·  SERVER {self.endpoint}" if self.endpoint else ""
        subtitle = (
            f"HAND {state.hand_id or '—'}  ·  {state.phase.upper()}  ·  "
            f"{state.street or 'LOBBY'}  ·  POT {state.pot}{endpoint}{countdown}"
        )
        self._center(screen, 1, subtitle, limit, self.color("white"))
        self._line(screen, 2, 2, "─" * max(1, limit - 4), self.color("blue"), limit)

    def _table(
        self, screen: curses.window, state: TableState, player_id: str, limit: int, height: int
    ) -> None:
        others = [player for player in state.players if player.player_id != player_id]
        mine = next((player for player in state.players if player.player_id == player_id), None)
        top = 5
        board_lines = CardRenderer.row(state.board)
        table_left = 4
        table_right = max(table_left + 24, limit - 4)
        table_width = table_right - table_left
        self._line(
            screen,
            top - 1,
            table_left,
            "┌" + "─" * max(1, table_width - 2) + "┐",
            self.color("blue"),
            limit,
        )
        self._center(screen, top, "COMMUNITY BOARD", limit, curses.A_BOLD | self.color("green"))
        for offset, line in enumerate(board_lines, top + 1):
            self._center(
                screen,
                offset,
                line,
                limit,
                (
                    self.color("red")
                    if any(symbol in line for symbol in ("♥", "♦"))
                    else self.color("white")
                ),
            )
        board_end = top + 1 + len(board_lines)
        self._center(
            screen,
            board_end,
            f"POT {state.pot:,}  ·  CURRENT {state.current_player or '—'}",
            limit,
            self.color("yellow"),
        )
        self._line(
            screen,
            board_end + 1,
            table_left,
            "└" + "─" * max(1, table_width - 2) + "┘",
            self.color("blue"),
            limit,
        )

        # Seat map: opponents occupy the top and sides; the controlled player is always bottom.
        positions = self._seat_positions(
            len(others), table_left, table_right, board_end + 4, max(1, height - 10)
        )
        for player, (row, column) in zip(others, positions, strict=True):
            self._seat_card(
                screen, player, row, column, player.player_id == state.current_player, limit
            )
        bottom_row = min(height - 8, board_end + 8)
        if mine is not None:
            self._seat_card(
                screen,
                mine,
                bottom_row,
                max(table_left, (limit - 22) // 2),
                mine.player_id == state.current_player,
                limit,
                mine=True,
            )
        else:
            self._center(
                screen,
                bottom_row,
                "YOUR SEAT — not joined",
                limit,
                self.color("green") | curses.A_BOLD,
            )

    @staticmethod
    def _seat_positions(
        count: int, left: int, right: int, top: int, span: int
    ) -> list[tuple[int, int]]:
        if count == 0:
            return []
        positions: list[tuple[int, int]] = []
        top_count = min(count, max(1, count // 2))
        for index in range(top_count):
            column = left + ((right - left) * (index + 1)) // (top_count + 1)
            positions.append((top, column - 10))
        remaining = count - top_count
        for index in range(remaining):
            row = top + 2 + (span * (index + 1)) // (remaining + 1)
            column = left - 1 if index % 2 == 0 else right - 10
            positions.append((row, column))
        return positions

    def _seat_card(
        self,
        screen: curses.window,
        player: PlayerState,
        row: int,
        column: int,
        active: bool,
        limit: int,
        *,
        mine: bool = False,
    ) -> None:
        width = 21
        color = (
            self.color("yellow") if active else self.color("green") if mine else self.color("white")
        )
        label = f" {player.player_id}{'  YOU' if mine else ''} "
        details = f"stack {player.stack:,}"
        status = player.status or "waiting"
        cards = (
            "cards hidden"
            if not mine or player.cards is None
            else "  ".join(CardRenderer.label(card) for card in player.cards)
        )
        self._line(screen, row, column, "┌" + "─" * (width - 2) + "┐", color, limit)
        self._line(
            screen,
            row + 1,
            column,
            f"│{label[: width - 2]:^{width - 2}}│",
            color | curses.A_BOLD,
            limit,
        )
        self._line(screen, row + 2, column, f"│{details:^{width - 2}}│", color, limit)
        self._line(screen, row + 3, column, f"│{cards[: width - 2]:^{width - 2}}│", color, limit)
        self._line(screen, row + 4, column, f"│{status:^{width - 2}}│", color, limit)
        self._line(screen, row + 5, column, "└" + "─" * (width - 2) + "┘", color, limit)

    def _controls(
        self,
        screen: curses.window,
        state: TableState,
        legal: LegalState | None,
        player_id: str,
        limit: int,
        height: int,
    ) -> None:
        row = max(height - 5, 0)
        if state.current_player == player_id:
            prompt = f"YOUR TURN  ·  {self._legal_prompt(legal)}"
            self._center(screen, row, prompt, limit, curses.A_BOLD | self.color("yellow"))
        else:
            self._center(
                screen,
                row,
                f"WAITING  ·  {state.current_player or 'table setup'}",
                limit,
                self.color("white"),
            )
        self._center(
            screen,
            row + 1,
            "ENTER play  ·  S start  ·  F2 history  ·  R refresh  ·  Q quit",
            limit,
            self.color("cyan"),
        )

    def _history(self, screen: curses.window, split: int, height: int) -> None:
        _, width = screen.getmaxyx()
        right = split + 1
        self._line(screen, 0, right, " ACTIVITY ", curses.A_BOLD | self.color("magenta"), width)
        self._line(screen, 1, right, "─" * max(1, width - right - 1), self.color("magenta"), width)
        for row, message in enumerate(self.history.visible(max(1, height - 3)), 2):
            self._line(screen, row, right, message, self.color("white"), width)

    @staticmethod
    def _player(player: PlayerState | None, current: str | None) -> str:
        if player is None:
            return "—"
        marker = " ◀" if player.player_id == current else ""
        return f"{player.player_id}: {player.stack}{marker}"

    @staticmethod
    def _legal_prompt(legal: LegalState | None) -> str:
        if legal is None:
            return "legal actions unavailable"
        return "  /  ".join(action_label(name) for name in legal.types) or "none"

    @staticmethod
    def _center(screen: curses.window, row: int, text: str, limit: int, attr: int) -> None:
        column = max(1, (limit - len(text)) // 2)
        TerminalView._line(screen, row, column, text, attr, limit)

    @staticmethod
    def _line(
        screen: curses.window, row: int, column: int, text: str, attr: int, limit: int
    ) -> None:
        if row < 0 or row >= screen.getmaxyx()[0] or column >= limit:
            return
        with suppress(curses.error):
            screen.addnstr(row, column, text, max(1, limit - column - 1), attr)


class ActionPicker:
    """Keyboard strategy for selecting a legal action and amount."""

    def __init__(self, history: HistoryLog, view: TerminalView) -> None:
        self.history = history
        self.view = view

    def pick(
        self, screen: curses.window, state: TableState, legal: LegalState, player_id: str
    ) -> dict[str, object] | None:
        choices = list(legal.types)
        if not choices:
            return None
        index = 0
        amounts: dict[str, int] = {}
        for name, bounds in (("bet_to", legal.bet_to), ("raise_to", legal.raise_to)):
            if bounds is not None:
                amounts[name] = bounds[0]
        if legal.raise_to is not None:
            amounts["all_in"] = legal.raise_to[1]
        elif legal.bet_to is not None:
            amounts["all_in"] = legal.bet_to[1]
        screen.timeout(-1)
        while True:
            self.view.draw(screen, state, player_id, legal)
            height, width = screen.getmaxyx()
            label = "  ".join(
                f"{'▶' if pos == index else ' '} {action_label(name)}"
                + (f" {amounts[name]}" if name in amounts else "")
                for pos, name in enumerate(choices)
            )
            self.view._center(
                screen, height - 3, label, width - 2, curses.A_BOLD | self.view.color("yellow")
            )
            self.view._center(
                screen,
                height - 2,
                "←/→ choose · ↑/↓ amount · ENTER play · Q back",
                width - 2,
                curses.A_BOLD | self.view.color("cyan"),
            )
            screen.refresh()
            key = screen.getch()
            if key in (ord("q"), ord("Q")):
                return None
            if key in (ord("a"), ord("A")) and "all_in" in choices:
                return self._all_in_command(legal)
            if key in (curses.KEY_F2, ord("h"), ord("H")):
                self._history_mode(screen)
                continue
            if key in (curses.KEY_LEFT, curses.KEY_UP):
                if key == curses.KEY_LEFT:
                    index = (index - 1) % len(choices)
                else:
                    self._change_amount(choices[index], amounts, legal, 10)
            elif key in (curses.KEY_RIGHT, curses.KEY_DOWN):
                if key == curses.KEY_RIGHT:
                    index = (index + 1) % len(choices)
                else:
                    self._change_amount(choices[index], amounts, legal, -10)
            elif key in (curses.KEY_ENTER, 10, 13):
                name = choices[index]
                if name == "all_in":
                    return self._all_in_command(legal)
                result: dict[str, object] = {"op": "action", "action": name}
                if name in amounts:
                    result["total"] = amounts[name]
                return result

    @staticmethod
    def _all_in_command(legal: LegalState) -> dict[str, object]:
        if legal.raise_to is not None:
            return {"op": "action", "action": "raise_to", "total": legal.raise_to[1]}
        if legal.bet_to is not None:
            return {"op": "action", "action": "bet_to", "total": legal.bet_to[1]}
        raise ServerError("ALL IN is no longer legal; refresh the table")

    @staticmethod
    def _change_amount(name: str, amounts: dict[str, int], legal: LegalState, delta: int) -> None:
        bounds = (
            legal.bet_to if name == "bet_to" else legal.raise_to if name == "raise_to" else None
        )
        if bounds is not None and name in amounts:
            amounts[name] = max(bounds[0], min(bounds[1], amounts[name] + delta))

    def _history_mode(self, screen: curses.window) -> None:
        screen.timeout(-1)
        while True:
            screen.erase()
            height, width = screen.getmaxyx()
            self.view._line(
                screen, 0, 2, "HISTORY MODE · ↑/↓ scroll · ENTER/F2 return", curses.A_BOLD, width
            )
            for row, message in enumerate(self.history.visible(max(1, height - 2)), 2):
                self.view._line(screen, row, 2, message, 0, width)
            screen.refresh()
            key = screen.getch()
            if key in (curses.KEY_UP, curses.KEY_PPAGE):
                self.history.scroll(-1 if key == curses.KEY_UP else -5, height - 2)
            elif key in (curses.KEY_DOWN, curses.KEY_NPAGE):
                self.history.scroll(1 if key == curses.KEY_DOWN else 5, height - 2)
            elif key in (curses.KEY_F2, curses.KEY_ENTER, 10, 13):
                return


class AdminMenu:
    """Interactive authenticated lobby configuration for local demonstrations."""

    def run(self, screen: curses.window, protocol: PokerProtocol, history: HistoryLog) -> None:
        token = self._prompt(screen, "Admin token:")
        if token is None:
            return
        fields = (
            ("small_blind", "Small blind", 50),
            ("big_blind", "Big blind", 100),
            ("minimum_players", "Minimum players", 2),
            ("maximum_players", "Maximum players", 10),
            ("auto_start_delay", "Auto-start seconds", 15),
            ("minimum_buy_in", "Minimum buy-in", 100),
            ("maximum_buy_in", "Maximum buy-in", 10_000),
        )
        config: dict[str, object] = {}
        for key, label, default in fields:
            value = self._prompt(screen, f"{label} [{default}]:")
            try:
                config[key] = (
                    float(value or default) if key == "auto_start_delay" else int(value or default)
                )
            except ValueError:
                history.add(f"ADMIN ERROR: invalid {label.lower()}")
                return
        try:
            protocol.admin_create_game(token, config)
            history.add("ADMIN: new game created")
            self._dashboard(screen, protocol, history)
        except ServerError as error:
            history.add(f"ADMIN ERROR: {error}")

    def _dashboard(
        self, screen: curses.window, protocol: PokerProtocol, history: HistoryLog
    ) -> None:
        screen.timeout(500)
        while True:
            try:
                state = protocol.state()
                error = ""
            except ServerError as exc:
                state = None
                error = str(exc)
            screen.erase()
            height, width = screen.getmaxyx()
            title = "♠  ADMIN CONTROL ROOM  ♠"
            screen.addnstr(
                1, max(0, (width - len(title)) // 2), title, max(1, width - 2), curses.A_BOLD
            )
            if state is None:
                lines = ["CONNECTION ERROR", error or "state unavailable", "R retry · Q close"]
            else:
                lines = [
                    f"SERVER {protocol.transport.host}:{protocol.transport.port}",
                    f"{state.phase.upper()} · HAND {state.hand_id or '—'} · POT {state.pot:,}",
                    f"PLAYERS {len(state.players)} · FREE SEATS {len(state.available_seats)}",
                    "",
                ]
                lines.extend(
                    f"SEAT {player.seat}  {player.player_id}  ·  stack {player.stack:,}  ·  "
                    f"{player.status or 'waiting'}"
                    for player in state.players
                )
                lines.append("")
                lines.append("R refresh · Q close")
            top = max(3, (height - len(lines)) // 2)
            for index, line in enumerate(lines):
                attr = curses.A_BOLD if index == 0 else 0
                screen.addnstr(
                    top + index, max(0, (width - len(line)) // 2), line, max(1, width - 2), attr
                )
            screen.refresh()
            key = screen.getch()
            if key in (ord("q"), ord("Q")):
                return
            if key in (ord("r"), ord("R")):
                continue

    @staticmethod
    def _prompt(screen: curses.window, label: str) -> str | None:
        curses.echo()
        try:
            screen.erase()
            height, width = screen.getmaxyx()
            center = width // 2
            title = "POKERENGINE ADMIN"
            title_column = max(0, center - len(title) // 2)
            label_column = max(0, center - len(label) // 2)
            box_width = min(40, max(24, width - 8))
            box_left = max(1, center - box_width // 2)
            screen.addnstr(1, title_column, title, max(1, width - title_column - 1), curses.A_BOLD)
            screen.addnstr(3, label_column, label, max(1, width - label_column - 1), curses.A_BOLD)
            screen.addnstr(
                5, box_left, "┌" + "─" * (box_width - 2) + "┐", max(1, width - box_left - 1)
            )
            screen.addnstr(
                6, box_left, "│" + " " * (box_width - 2) + "│", max(1, width - box_left - 1)
            )
            screen.addnstr(
                7, box_left, "└" + "─" * (box_width - 2) + "┘", max(1, width - box_left - 1)
            )
            screen.addnstr(
                min(height - 2, 9),
                max(0, center - 21),
                "ENTER keeps the default where available",
                max(1, width - max(0, center - 21) - 1),
                curses.A_DIM,
            )
            screen.refresh()
            raw = screen.getstr(6, box_left + 2, max(1, box_width - 4))
            value = raw.decode("utf-8", errors="ignore").strip()
            return value or None
        finally:
            curses.noecho()


class PokerClient:
    """Controller coordinating transport, protocol, polling and terminal views."""

    def __init__(
        self,
        protocol: PokerProtocol,
        *,
        show_private: bool,
        admin_enabled: bool = False,
    ) -> None:
        self.protocol = protocol
        self.player_id = ""
        self.stack = 0
        self.show_private = show_private
        self.admin_enabled = admin_enabled
        self.history = HistoryLog()
        self.nickname_prompt = NicknamePrompt()
        self.seat_picker = SeatPicker()
        self.view = TerminalView(
            self.history, f"{protocol.transport.host}:{protocol.transport.port}"
        )
        self.picker = ActionPicker(self.history, self.view)
        self.admin_menu = AdminMenu()
        self.event_cursor = 0

    def run(
        self, screen: curses.window, player_id: str | None = None, seat: int = 0, stack: int = 1000
    ) -> None:
        if self.admin_enabled:
            self.admin_menu.run(screen, self.protocol, self.history)
            return
        if player_id is None:
            player_id = self.nickname_prompt.read(screen)
            lobby = self.protocol.state()
            stack = self.nickname_prompt.read_buy_in(
                screen, lobby.minimum_buy_in, lobby.maximum_buy_in
            )
            picked = self.seat_picker.pick(screen, lobby.available_seats)
            if picked is None:
                return
            seat = picked
        self.player_id = player_id
        self.stack = stack
        self.protocol.join(player_id, seat, stack)
        self.history.add(f"CONNECTED seat {seat}; synthetic chips")
        self.view.setup_colors(screen)
        with suppress(curses.error):
            curses.curs_set(0)
        screen.timeout(200)
        command: str | None = None
        state = self._empty_state()
        result = ""
        shortcuts = {
            "f": "fold",
            "c": "check",
            "l": "call",
            "a": "all_in",
            "s": "start",
            "n": "start",
            "d": "settle",
        }
        while True:
            legal = None
            try:
                self._sync_events()
                state = (
                    self.protocol.private_state() if self.show_private else self.protocol.state()
                )
                legal = self.protocol.legal() if state.current_player == self.player_id else None
                settlement = self.protocol.result().get("result")
                if isinstance(settlement, dict):
                    payouts = [
                        _wire_object(item, "result.payouts[]")
                        for item in _wire_array(settlement.get("payouts"), "result.payouts")
                    ]
                    result = (
                        "RESULT "
                        + ", ".join(f"{item['player_id']} +{item['amount']}" for item in payouts)
                        + f" | rake {settlement.get('rake')}"
                    )
                else:
                    result = ""
            except ServerError as error:
                message = f"CONNECTION/REQUEST: {error}"
                if not self.history.lines or self.history.lines[-1] != message:
                    self.history.add(message)
            self.view.draw(screen, state, self.player_id, legal, command=command, result=result)
            key = screen.getch()
            if key == -1 or key == curses.KEY_RESIZE:
                continue
            if command is not None:
                if key == 27:
                    command = None
                elif key in (curses.KEY_ENTER, 10, 13):
                    if command.strip() == "quit":
                        return
                    self._command(command, state)
                    command = None
                elif key in (curses.KEY_BACKSPACE, 127, 8):
                    command = command[:-1]
                elif 32 <= key <= 126 and len(command) < 80:
                    command += chr(key)
                continue
            if key in (ord("q"), ord("Q")):
                return
            if key in (ord(":"), curses.KEY_ENTER, 10, 13):
                command = ""
            elif 0 <= key <= 255 and chr(key).lower() in shortcuts:
                self._command(shortcuts[chr(key).lower()], state)

    def _command(self, command: str, state: TableState) -> None:
        try:
            payload = console_request(command)
            if payload is None:
                return
            if payload["op"] == "action" and state.current_player != self.player_id:
                raise ValueError("wait for your turn; the table keeps refreshing")
            response = self.protocol.transport.request(payload)
            events = _wire_strings(response.get("events", []), "response.events")
            self.history.add(f"{command}: {', '.join(events) or 'ok'}")
        except (ValueError, ServerError) as error:
            self.history.add(f"REJECTED [{command}]: {error}")

    def _sync_events(self) -> None:
        cursor, events = self.protocol.events(self.event_cursor)
        for event in events:
            self.history.add(f"event: {event}")
        self.event_cursor = cursor

    def _request(self, operation: str | dict[str, object]) -> None:
        try:
            if isinstance(operation, str):
                if operation not in {"start", "settle"}:
                    raise ValueError("unsupported client operation")
                response = self.protocol.start() if operation == "start" else self.protocol.settle()
                label = operation
            else:
                response = self.protocol.transport.request(operation)
                action = operation.get("action")
                label = action_label(action) if isinstance(action, str) else "action"
            events = _wire_strings(response.get("events"), "response.events")
            self.history.add(f"{label}: {', '.join(events) or 'ok'}")
        except ServerError as error:
            self.history.add(f"ERROR: {error}")

    @staticmethod
    def _empty_state() -> TableState:
        return TableState(0, "waiting", None, (), 1, 1_000_000, None, None, None, None, 0, (), ())


class ClientSetupUI:
    """Centered connection-mode setup shown before the table interface."""

    def run(self, screen: curses.window) -> tuple[str, int, bool, bool]:
        host = self._text(screen, "Server host", "127.0.0.1")
        while True:
            raw = self._text(screen, "Server port", "8765")
            try:
                port = int(raw)
            except ValueError:
                self._message(screen, "Port must be a whole number.")
                continue
            if 1 <= port <= 65535:
                break
            self._message(screen, "Port must be between 1 and 65535.")
        mode = self._text(screen, "Mode: player or admin", "player").lower()
        admin = mode in {"a", "admin"}
        private = (
            False
            if admin
            else self._text(screen, "Request private cards? (y/N)", "N").lower() in {"y", "yes"}
        )
        return host, port, admin, private

    def _text(self, screen: curses.window, label: str, default: str) -> str:
        curses.noecho()
        value = ""
        screen.timeout(-1)
        while True:
            screen.erase()
            height, width = screen.getmaxyx()
            title = "♠ ♥ ♦  POKERENGINE CLIENT SETUP  ♣ ♦ ♥ ♠"
            screen.addnstr(
                1, max(0, (width - len(title)) // 2), title, max(1, width - 2), curses.A_BOLD
            )
            label_column = max(0, (width - len(label)) // 2)
            screen.addnstr(
                height // 2 - 1,
                label_column,
                label,
                max(1, width - label_column - 1),
                curses.A_BOLD,
            )
            shown = value if value else f"{default}  (placeholder)"
            attr = curses.A_NORMAL if value else curses.A_DIM
            box_width = min(72, max(40, width - 8))
            left = max(1, (width - box_width) // 2)
            screen.addnstr(
                height // 2 + 1,
                left,
                f"{shown:^{box_width - 2}}",
                max(1, width - left - 1),
                attr,
            )
            screen.addnstr(
                height // 2 + 3,
                max(0, (width - 36) // 2),
                "Type to replace · Backspace clears · Enter confirms",
                max(1, width - 2),
                curses.A_DIM,
            )
            screen.refresh()
            key = screen.getch()
            if key in (curses.KEY_ENTER, 10, 13):
                return value or default
            if key in (curses.KEY_BACKSPACE, 127, 8):
                value = value[:-1]
            elif 32 <= key <= 126 and len(value) < 64:
                value += chr(key)

    @staticmethod
    def _message(screen: curses.window, message: str) -> None:
        screen.erase()
        height, width = screen.getmaxyx()
        screen.addnstr(
            height // 2,
            max(0, (width - len(message)) // 2),
            message,
            max(1, width - 2),
            curses.A_BOLD,
        )
        screen.refresh()
        screen.getch()


def run_curses(
    host: str,
    port: int,
    *,
    admin_enabled: bool,
    show_private: bool,
    player_id: str | None = None,
    seat: int = 0,
    stack: int = 1000,
) -> None:
    """Keep the existing terminal UI and explicit socket cleanup."""
    try:
        transport = TCPTransport(host, port)
    except ServerError as error:
        sys.stderr.write(f"\nConnection failed: {error}\n")
        return
    try:
        curses.wrapper(
            PokerClient(
                PokerProtocol(transport),
                show_private=show_private,
                admin_enabled=admin_enabled,
            ).run,
            player_id,
            seat,
            stack,
        )
    finally:
        transport.close()


def console_request(command: str) -> dict[str, object] | None:
    """Translate input only; the server/native engine validates legal actions."""
    words = command.split()
    if not words:
        return None
    if (
        words[0] in {"state", "private_state", "legal", "start", "settle", "result"}
        and len(words) == 1
    ):
        return {"op": words[0]}
    if words[0] == "events" and len(words) == 1:
        return {"op": "events", "cursor": 0}
    if words[0] in {"fold", "check", "call", "all_in"} and len(words) == 1:
        return {"op": "action", "action": words[0]}
    if words[0] in {"bet", "raise"} and len(words) == 2:
        return {"op": "action", "action": words[0] + "_to", "total": int(words[1])}
    raise ValueError(
        "Use state, private_state, legal, start, fold/check/call, bet/raise TOTAL, "
        "all_in, settle, result, events or quit."
    )


def run_console(host: str, port: int, *, player_id: str, seat: int, stack: int) -> None:
    """Human-readable line client over the same real TCP protocol."""
    transport = TCPTransport(host, port)
    try:
        protocol = PokerProtocol(transport)
        typer.echo(json.dumps(protocol.join(player_id, seat, stack)))
        typer.echo(
            "PokerEngine console: state | legal | start | call/check/fold | "
            "bet/raise TOTAL | settle | result | quit"
        )
        while True:
            try:
                command = input(f"{player_id}> ").strip()
            except EOFError, KeyboardInterrupt:
                break
            if command == "quit":
                break
            try:
                payload = console_request(command)
                if payload is not None:
                    typer.echo(json.dumps(transport.request(payload), indent=2))
            except (ValueError, ServerError) as error:
                typer.echo(f"Rejected: {error}")
    finally:
        transport.close()


app = typer.Typer(add_completion=False, pretty_exceptions_enable=False)


@app.command()
def connect(
    console: Annotated[
        bool, typer.Option(help="Use line commands instead of curses setup.")
    ] = False,
    host: str = "127.0.0.1",
    port: Annotated[int, typer.Option(min=1, max=65535)] = 8765,
    private: Annotated[
        bool, typer.Option(help="Request only this connection's player view.")
    ] = False,
    player_id: str | None = None,
    seat: Annotated[int, typer.Option(min=0)] = 0,
    stack: Annotated[int, typer.Option(min=1)] = 1000,
) -> None:
    """Connect the existing example client to the real local TCP server."""
    if console:
        if player_id is None or not player_id.strip():
            raise typer.BadParameter("--console requires --player-id.")
        run_console(host, port, player_id=player_id, seat=seat, stack=stack)
    elif player_id is not None:
        run_curses(
            host,
            port,
            admin_enabled=False,
            show_private=private,
            player_id=player_id,
            seat=seat,
            stack=stack,
        )
    else:
        host, port, admin, private = curses.wrapper(ClientSetupUI().run)
        run_curses(host, port, admin_enabled=admin, show_private=private)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
