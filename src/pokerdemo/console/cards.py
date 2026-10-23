"""Compact, recipient-safe card and player presentation for terminal clients."""

from __future__ import annotations

from typing import Literal

from rich.table import Table
from rich.text import Text
from textual.widgets import Static

from pokerdemo.protocol.models import PlayerState, Snapshot

_SUIT_GLYPHS = {"s": "♠", "h": "♥", "d": "♦", "c": "♣"}
_SUIT_CODES = {value: key for key, value in _SUIT_GLYPHS.items()}
_RED_SUITS = {"h", "d"}
_RANKS = {"2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"}
_CREAM = "#fff5dd"
_DARK = "#172b36"
_EMERALD = "#64e6b4"
_MUTED = "#91a6a5"
CardKind = Literal["face", "back", "empty"]


def _face(value: str) -> tuple[str, str, str]:
    """Read a display label, accepting engine shorthand and Unicode suit labels."""
    value = value.strip()
    if len(value) < 2:
        return "?", "?", _DARK
    suit = _SUIT_CODES.get(value[-1], value[-1].lower())
    rank = value[:-1].upper().replace("T", "10")
    if rank not in _RANKS or suit not in _SUIT_GLYPHS:
        return "?", "?", _DARK
    color = "#bb3346" if suit in _RED_SUITS else _DARK
    return rank, _SUIT_GLYPHS[suit], color


def _card(value: str | None, *, kind: CardKind, width: int) -> list[Text]:
    inner = width - 2
    if kind == "face" and value is not None:
        rank, suit, color = _face(value)
        edge = f"#c6baa1 on {_CREAM}"
        ink = f"bold {color} on {_CREAM}"
        rows = [Text("╭" + "─" * inner + "╮", style=edge)]
        for content in (rank.ljust(inner), suit.center(inner), rank.rjust(inner)):
            row = Text("│", style=edge)
            row.append(content, style=ink)
            row.append("│", style=edge)
            rows.append(row)
        rows.append(Text("╰" + "─" * inner + "╯", style=edge))
        return rows
    if kind == "back":
        edge = "#5ab9a7 on #163d39"
        fill = "#65c2af on #163d39"
        center = "◆".center(inner, "░")
        contents = ["░" * inner, center, "░" * inner]
    else:
        edge = "#496460"
        fill = "#496460"
        contents = [" " * inner, "·".center(inner), " " * inner]
    rows = [Text("╭" + "─" * inner + "╮", style=edge)]
    for content in contents:
        row = Text("│", style=edge)
        row.append(content, style=fill)
        row.append("│", style=edge)
        rows.append(row)
    rows.append(Text("╰" + "─" * inner + "╯", style=edge))
    return rows


def _row(values: list[str], *, slots: int, hidden: bool, width: int) -> list[Text]:
    cards = [
        _card(
            values[index] if index < len(values) else None,
            kind="face" if index < len(values) else "back" if hidden else "empty",
            width=width,
        )
        for index in range(slots)
    ]
    return [Text(" ").join(card[line] for card in cards) for line in range(5)]


def card_display(
    snapshot: Snapshot, player_id: str, width: int, *, completed: bool = False
) -> Text:
    """Render only the recipient's cards plus the public community board."""
    state = snapshot.state
    own = next((player for player in state.players if player.player_id == player_id), None)
    hand = list(own.cards or []) if own else []
    dealt = state.hand_id is not None and state.phase not in {"waiting", "ready"}
    # The server's recipient-specific snapshot is the only source of hole cards.
    # Concealed backs never use another player's cards, seeds or engine internals.
    hidden = dealt and not hand
    compact = width < 57
    card_width = 5 if compact else 7
    hand_width = card_width * 2 + 1
    board_width = card_width * 5 + 4
    hand_lines = _row(hand, slots=2, hidden=hidden, width=card_width)
    board_lines = _row(list(state.board), slots=5, hidden=False, width=card_width)
    hand_caption = "Private to you" if hand else "Cards hidden" if hidden else "Not dealt yet"
    board_caption = (
        (state.street or "").replace("_", " ").title() if state.board else "Waiting for the flop"
    )
    if completed:
        board_caption = "Last hand · " + board_caption
    result = Text()
    if width >= hand_width + board_width + 3:
        result.append("YOUR HAND".ljust(hand_width), style=f"bold {_EMERALD}")
        result.append("   ")
        result.append("COMMUNITY CARDS", style=f"bold {_MUTED}")
        result.append("\n")
        for own_line, board_line in zip(hand_lines, board_lines, strict=True):
            result.append_text(own_line)
            result.append("   ")
            result.append_text(board_line)
            result.append("\n")
        # Compact cards leave 11 columns; use a short, unambiguous privacy caption.
        caption = (
            hand_caption
            if len(hand_caption) <= hand_width
            else "Private"
            if hand
            else "Hidden"
            if hidden
            else "Not dealt"
        )
        result.append(caption.ljust(hand_width), style=_MUTED)
        result.append("   ")
        result.append(board_caption, style=_MUTED)
    else:
        # Very narrow windows stack the two groups instead of clipping card ranks.
        result.append("YOUR HAND", style=f"bold {_EMERALD}")
        result.append("\n")
        for line in hand_lines:
            result.append_text(line)
            result.append("\n")
        result.append(hand_caption + "\n", style=_MUTED)
        result.append("COMMUNITY CARDS", style=f"bold {_MUTED}")
        result.append("\n")
        for line in board_lines:
            result.append_text(line)
            result.append("\n")
        result.append(board_caption, style=_MUTED)
    return result


class CardsView(Static):
    """Seven terminal rows at normal client widths; redraw after a terminal resize."""

    _snapshot: Snapshot | None = None
    _player_id = ""
    _last_dealt: Snapshot | None = None

    def show(self, snapshot: Snapshot, player_id: str) -> None:
        if player_id != self._player_id or snapshot.state.phase in {"ready", "waiting"}:
            self._last_dealt = None
        self._snapshot = snapshot
        self._player_id = player_id
        own = next((p for p in snapshot.state.players if p.player_id == player_id), None)
        if own and own.cards and snapshot.state.phase in {"betting", "showdown"}:
            self._last_dealt = snapshot
        completed = snapshot.state.phase == "finished"
        # Retain only cards this recipient already saw, so settlement stays legible.
        shown = self._last_dealt if completed and self._last_dealt else snapshot
        self.update(card_display(shown, player_id, self.content_size.width, completed=completed))

    def on_resize(self) -> None:
        if self._snapshot is not None:
            self.show(self._snapshot, self._player_id)


def _player_label(player: PlayerState, snapshot: Snapshot, player_id: str) -> Text:
    active = player.player_id == snapshot.state.current_player
    label = Text("▶ " if active else "  ", style=f"bold {_EMERALD}" if active else _MUTED)
    label.append(player.player_id, style="bold" if player.player_id == player_id else "")
    if player.player_id == player_id:
        label.append(" · YOU", style=_EMERALD)
    if player.seat == snapshot.state.dealer_seat:
        label.append(" · D", style="#e6bf78")
    return label


class PlayerTableView(Static):
    """Small public seat list with an explicit current-turn and own-seat marker."""

    def show(self, snapshot: Snapshot, player_id: str) -> None:
        table = Table(expand=True, box=None, padding=(0, 1), header_style=f"bold {_MUTED}")
        table.add_column("PLAYER", ratio=1, overflow="ellipsis", no_wrap=True)
        table.add_column("STACK", justify="right", width=8)
        table.add_column("BET", justify="right", width=6)
        table.add_column("STATE", width=10, overflow="ellipsis", no_wrap=True)
        for player in snapshot.state.players:
            active = player.player_id == snapshot.state.current_player
            state = "away" if not player.connected else (player.status or "ready")
            state = "YOUR TURN" if active and player.player_id == player_id else state
            table.add_row(
                _player_label(player, snapshot, player_id),
                f"{player.stack:,}",
                f"{player.street_committed:,}" if player.street_committed else "—",
                Text(state.replace("_", " ").upper()),
                style="on #12372f" if active else None,
            )
        self.update(table)
