"""Versioned WebSocket messages shared by the server, consoles and recorder."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator


class Model(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)


class Join(Model):
    op: Literal["join"]
    player_id: str = Field(min_length=1, max_length=32, pattern=r"^[\x20-\x7e]+$")
    seat: int = Field(ge=0, le=9)
    stack: int = Field(default=1000, ge=1)


class ActionRequest(Model):
    op: Literal["action"]
    action: Literal["fold", "check", "call", "bet_to", "raise_to", "all_in"]
    total: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def total_matches_action(self) -> Self:
        if (self.action in {"bet_to", "raise_to"}) != (self.total is not None):
            raise ValueError("only bet_to and raise_to require a total")
        return self


class Query(Model):
    op: Literal[
        "leave", "start", "settle", "result", "legal", "state", "public_state", "private_state"
    ]


class Events(Model):
    op: Literal["events"]
    cursor: int = Field(ge=0)


class Subscribe(Model):
    op: Literal["subscribe"]
    private: bool = False


class CreateGame(Model):
    op: Literal["admin_create_game"]
    admin_token: str = Field(repr=False)
    small_blind: int = Field(ge=1)
    big_blind: int = Field(ge=1)
    minimum_players: int = Field(ge=2)
    maximum_players: int = Field(ge=2)
    auto_start_delay: float = Field(gt=0, allow_inf_nan=False)
    minimum_buy_in: int = Field(ge=1)
    maximum_buy_in: int = Field(ge=1)
    seed: int | None = None


Request = Annotated[
    Join | ActionRequest | Query | Events | Subscribe | CreateGame, Field(discriminator="op")
]
request_adapter = TypeAdapter(Request)


class Command(Model):
    id: int = Field(ge=1)
    request: Request


class AmountRange(Model):
    minimum: int
    maximum: int


class PlayerState(Model):
    player_id: str
    seat: int
    connected: bool
    stack: int
    street_committed: int
    total_contributed: int
    status: str | None
    cards: list[str] | None


class TableState(Model):
    version: int
    phase: str
    hand_id: int | None
    dealer_seat: int | None
    street: str | None
    current_player: str | None
    connected_players: list[str]
    pot: int
    buy_in: AmountRange
    available_seats: list[int]
    board: list[str]
    players: list[PlayerState]
    auto_start_seconds: int | None

    @classmethod
    def from_dict(cls, value: object) -> Self:
        return cls.model_validate(value)


class LegalState(Model):
    types: list[str]
    call: int | None
    bet_to: AmountRange | None
    raise_to: AmountRange | None

    @classmethod
    def from_dict(cls, value: object) -> Self | None:
        return None if value is None else cls.model_validate(value)


class Payout(Model):
    player_id: str
    amount: int


class Settlement(Model):
    payouts: list[Payout]
    rake: int


class Hello(Model):
    type: Literal["hello"] = "hello"
    protocol: Literal[1] = 1
    server: Literal["pokerDemo"] = "pokerDemo"
    private_views: bool


class Reply(Model):
    type: Literal["reply"] = "reply"
    id: int | None
    ok: bool
    error: str | None = None
    code: str | None = None
    state: TableState | None = None
    legal_actions: LegalState | None = None
    result: Settlement | None = None
    identity: str | None = None
    cursor: int | None = None
    version: int | None = None
    events: list[str] = Field(default_factory=list)
    resumed: bool = False
    away: bool = False
    admin: bool = False
    subscribed: bool = False


class Snapshot(Model):
    type: Literal["snapshot"] = "snapshot"
    state: TableState
    legal_actions: LegalState | None
    result: Settlement | None
    activity: list[str]


server_message = TypeAdapter(Annotated[Reply | Snapshot, Field(discriminator="type")])
