"""Thread-safe game application; all poker rules belong to the installed engine."""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass

from pokerengine import Action, ActionType, AmountRange, CommandRecord, GameView, PokerGame
from pydantic import BaseModel, ConfigDict, Field, model_validator

from pokerdemo.protocol.models import request_adapter


class ServerConfig(BaseModel):
    """Configuration for the example server and its initial table."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)

    small_blind: int = Field(default=50, ge=1)
    big_blind: int = Field(default=100, ge=1)
    seed: int | None = None
    minimum_players: int = Field(default=2, ge=2, le=10)
    maximum_players: int = Field(default=10, ge=2, le=10)
    auto_start_delay: float = Field(default=15.0, gt=0, allow_inf_nan=False)
    action_timeout: float = Field(default=8.0, gt=0, allow_inf_nan=False)
    minimum_buy_in: int = Field(default=100, ge=1)
    maximum_buy_in: int = Field(default=10_000, ge=1)
    admin_token: str | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def ordered_limits(self):
        if self.big_blind < self.small_blind:
            raise ValueError("big_blind must be >= small_blind")
        if self.maximum_players < self.minimum_players:
            raise ValueError("maximum_players must be >= minimum_players")
        if self.maximum_buy_in < self.minimum_buy_in:
            raise ValueError("maximum_buy_in must be >= minimum_buy_in")
        return self


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
        request = request_adapter.validate_python(request).model_dump(exclude_none=True)
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

    def snapshot(
        self, identity: str | None, connection_id: int, *, private: bool = False
    ) -> dict[str, object]:
        """Create one atomic recipient-specific projection; never share private snapshots."""
        with self._lock:
            state, _ = self.handle(
                {"op": "private_state" if private else "state"}, identity, connection_id
            )
            legal, _ = self.handle({"op": "legal"}, identity, connection_id)
            result, _ = self.handle({"op": "result"}, identity, connection_id)
            return {
                "state": state["state"],
                "legal_actions": legal["legal_actions"],
                "result": result["result"],
                "activity": list(self.activity),
            }

    def operator_command(self, command: str) -> None:
        """Local server console controls lifecycle without impersonating a player."""
        with self._lock:
            if command == "start":
                self.game.start_hand()
                self._auto_start_deadline = None
                self.activity.append(f"OPERATOR started hand {self.game.public_view().hand_id}")
            elif command == "settle":
                self.game.settle_showdown()
                self.activity.append("OPERATOR settled showdown")
            else:
                raise ValueError("Operator supports start and settle only")

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
        """Run the server-owned timeout policy from the WebSocket host timer."""
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
