"""Observable WebSocket contracts against the installed native PokerEngine."""

from __future__ import annotations

import contextlib
import json
import math
import threading
import time
from collections.abc import Callable, Iterator

import pytest
from pokerengine import PokerError
from pydantic import ValidationError
from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.sync.client import ClientConnection, connect

from pokerdemo.game.service import PokerApplication, ServerConfig
from pokerdemo.network.client import ServerError, WebSocketTransport
from pokerdemo.network.server import PokerWebSocketServer
from pokerdemo.protocol.models import LegalState, Snapshot, TableState


@pytest.fixture
def application() -> PokerApplication:
    return PokerApplication(ServerConfig(seed=42, auto_start_delay=60), allow_private=True)


@contextlib.contextmanager
def serving(application: PokerApplication) -> Iterator[PokerWebSocketServer]:
    with PokerWebSocketServer(("127.0.0.1", 0), application) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join(timeout=3)
            assert not thread.is_alive(), "WebSocket listener did not stop"


def raw_peer(server: PokerWebSocketServer, **options: object) -> ClientConnection:
    host, port = server.server_address
    return connect(
        f"ws://{host}:{port}/ws",
        proxy=None,
        open_timeout=2,
        close_timeout=1,
        **options,
    )


def receive_object(peer: ClientConnection) -> dict[str, object]:
    message = peer.recv(timeout=2)
    assert isinstance(message, str), "The protocol must send JSON text messages"
    value = json.loads(message)
    assert isinstance(value, dict)
    return value


def wait_snapshot(transport: WebSocketTransport, predicate: Callable[[Snapshot], bool]) -> Snapshot:
    """Observe pushed messages; do not issue a state request in this helper."""
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        snapshot = transport.snapshot
        if snapshot is not None and predicate(snapshot):
            return snapshot
        time.sleep(0.01)
    pytest.fail("The subscribed client did not receive the expected pushed snapshot")


def join(application: PokerApplication, name: str, seat: int, connection: int) -> str:
    response, identity = application.handle(
        {"op": "join", "player_id": name, "seat": seat, "stack": 1000}, None, connection
    )
    assert response["ok"] is True and identity == name
    return name


def test_websocket_push_privacy_turn_and_recovery(application: PokerApplication) -> None:
    with (
        serving(application) as server,
        contextlib.closing(WebSocketTransport(*server.server_address)) as alice,
        contextlib.closing(WebSocketTransport(*server.server_address)) as bob,
        contextlib.closing(WebSocketTransport(*server.server_address)) as observer,
    ):
        observer.subscribe()
        wait_snapshot(observer, lambda snapshot: snapshot.state.phase == "waiting")
        alice.request({"op": "join", "player_id": "alice", "seat": 0, "stack": 1000})
        bob.request({"op": "join", "player_id": "bob", "seat": 1, "stack": 1000})
        alice.subscribe(private=True)
        bob.subscribe(private=True)
        ready = wait_snapshot(observer, lambda snapshot: snapshot.state.phase == "ready")
        assert set(ready.state.connected_players) == {"alice", "bob"}
        assert len(ready.state.available_seats) == 8

        alice.request({"op": "start"})
        public = wait_snapshot(observer, lambda snapshot: snapshot.state.phase == "betting")
        private = wait_snapshot(alice, lambda snapshot: snapshot.state.phase == "betting")
        other_private = wait_snapshot(bob, lambda snapshot: snapshot.state.phase == "betting")
        assert all(player.cards is None for player in public.state.players)
        mine = next(player for player in private.state.players if player.player_id == "alice")
        opponent = next(player for player in private.state.players if player.player_id == "bob")
        hidden = next(
            player for player in other_private.state.players if player.player_id == "alice"
        )
        assert len(mine.cards or []) == 2
        assert opponent.cards is None and hidden.cards is None
        with pytest.raises(ServerError):
            observer.request({"op": "private_state"})
        with pytest.raises(ServerError):
            observer.subscribe(private=True)

        current = alice if public.state.current_player == "alice" else bob
        other = bob if current is alice else alice
        before = public.state.version, application.game.event_cursor
        with pytest.raises(ServerError):
            other.request({"op": "action", "action": "call"})
        with pytest.raises(ServerError):
            alice.request({"op": "join", "player_id": "bob", "seat": 1, "stack": 1000})
        assert (application.game.state_version, application.game.event_cursor) == before
        current.request({"op": "action", "action": "call"})
        changed = wait_snapshot(observer, lambda snapshot: snapshot.state.version > before[0])
        assert all(player.cards is None for player in changed.state.players)
        assert observer.request({"op": "state"})["ok"] is True
        assert "HandStarted" in observer.request({"op": "events", "cursor": 0})["events"]


@pytest.mark.parametrize(
    "message",
    [
        "not-json",
        "[]",
        "{}",
        '{"id":4,"request":{"op":"unknown"}}',
        '{"id":5,"request":{"op":"action","action":"bet_to","total":true}}',
        '{"id":6,"request":{"op":"events","cursor":-1}}',
        '{"id":7,"request":{"op":"state","admin_token":"do-not-echo-this"}}',
    ],
)
def test_websocket_malformed_request_is_local_and_recoverable(
    application: PokerApplication, message: str
) -> None:
    before = application.game.state_version, application.game.event_cursor
    with serving(application) as server, raw_peer(server) as peer:
        assert receive_object(peer)["type"] == "hello"
        peer.send(message)
        rejected = receive_object(peer)
        assert rejected["type"] == "reply" and rejected["ok"] is False
        assert rejected["code"] == "invalid_request"
        assert "do-not-echo-this" not in json.dumps(rejected)
        assert (application.game.state_version, application.game.event_cursor) == before
        peer.send(json.dumps({"id": 99, "request": {"op": "state"}}))
        recovered = receive_object(peer)
        assert recovered["type"] == "reply" and recovered["id"] == 99
        assert recovered["ok"] is True


@pytest.mark.parametrize(
    "message, code",
    [(b"binary-is-not-json-text", 1003), ("x" * (64 * 1024 + 1), 1009)],
)
def test_websocket_binary_and_oversize_close_only_the_offending_peer(
    application: PokerApplication, message: str | bytes, code: int
) -> None:
    with serving(application) as server:
        with raw_peer(server) as peer:
            assert receive_object(peer)["type"] == "hello"
            peer.send(message)
            with pytest.raises(ConnectionClosed) as closed:
                peer.recv(timeout=2)
            assert closed.value.rcvd is not None
            assert closed.value.rcvd.code == code
        with contextlib.closing(WebSocketTransport(*server.server_address)) as unaffected:
            assert unaffected.request({"op": "state"})["ok"] is True


def test_websocket_rejects_foreign_origin_before_join(application: PokerApplication) -> None:
    with serving(application) as server:
        with pytest.raises(InvalidStatus) as rejected:
            with raw_peer(server, origin="https://foreign.invalid"):
                pytest.fail("An unrelated browser origin was accepted")
        assert rejected.value.response.status_code == 403
        assert not application.game.public_view().players
        with contextlib.closing(WebSocketTransport(*server.server_address)) as local:
            assert local.request({"op": "state"})["ok"] is True


def test_websocket_reconnect_revokes_old_connection_without_detaching_new_one(
    application: PokerApplication,
) -> None:
    with (
        serving(application) as server,
        contextlib.closing(WebSocketTransport(*server.server_address)) as old,
        contextlib.closing(WebSocketTransport(*server.server_address)) as resumed,
        contextlib.closing(WebSocketTransport(*server.server_address)) as observer,
    ):
        old.request({"op": "join", "player_id": "alice", "seat": 0, "stack": 1000})
        old.subscribe(private=True)
        observer.subscribe()
        response = resumed.request({"op": "join", "player_id": "alice", "seat": 0})
        assert response["resumed"] is True
        with pytest.raises(ServerError):
            old.request({"op": "private_state"})
        old.close()
        assert resumed.request({"op": "private_state"})["ok"] is True
        connected = wait_snapshot(
            observer, lambda snapshot: "alice" in snapshot.state.connected_players
        )
        assert connected.state.connected_players == ["alice"]
        resumed.close()
        wait_snapshot(observer, lambda snapshot: "alice" not in snapshot.state.connected_players)


def test_websocket_graceful_shutdown_closes_listener_and_clients(
    application: PokerApplication,
) -> None:
    with serving(application) as server:
        with contextlib.closing(WebSocketTransport(*server.server_address)) as client:
            client.request({"op": "join", "player_id": "alice", "seat": 0, "stack": 1000})
            client.subscribe(private=True)
            server.shutdown()
            with pytest.raises(ServerError):
                client.request({"op": "state"})
            with pytest.raises(OSError):
                with raw_peer(server):
                    pytest.fail("The server accepted a connection after shutdown")
        assert not application.dashboard_snapshot()[1]


def test_two_websocket_clients_finish_hand_without_private_result_leak(
    application: PokerApplication,
) -> None:
    with (
        serving(application) as server,
        contextlib.closing(WebSocketTransport(*server.server_address)) as alice,
        contextlib.closing(WebSocketTransport(*server.server_address)) as bob,
    ):
        alice.request({"op": "join", "player_id": "alice", "seat": 0, "stack": 1000})
        bob.request({"op": "join", "player_id": "bob", "seat": 1, "stack": 1000})
        alice.subscribe()
        bob.subscribe()
        alice.request({"op": "start"})
        state = TableState.from_dict(alice.request({"op": "state"})["state"])
        current = alice if state.current_player == "alice" else bob
        other = bob if current is alice else alice
        before = application.game.state_version, application.game.event_cursor
        with pytest.raises(ServerError):
            current.request({"op": "action", "action": "check"})
        assert (application.game.state_version, application.game.event_cursor) == before
        current.request({"op": "action", "action": "all_in"})
        other.request({"op": "action", "action": "call"})
        assert application.game.phase.value == "showdown"
        alice.request({"op": "settle"})
        result = alice.request({"op": "result"})["result"]
        assert isinstance(result, dict) and set(result) == {"payouts", "rake"}
        assert sum(payout["amount"] for payout in result["payouts"]) == 2000
        finished = wait_snapshot(bob, lambda snapshot: snapshot.state.phase == "finished")
        assert all(player.cards is None for player in finished.state.players)
        assert sum(player.stack for player in finished.state.players) == 2000
        assert "HandFinished" in alice.request({"op": "events", "cursor": 0})["events"]


def test_reconnect_invalidates_old_connection_and_old_detach_is_safe(
    application: PokerApplication,
) -> None:
    join(application, "alice", 0, 1)
    response, identity = application.handle(
        {"op": "join", "player_id": "alice", "seat": 0}, None, 2
    )
    assert response["resumed"] and identity == "alice"
    application.detach("alice", 1)
    assert application.handle({"op": "state"}, "alice", 2)[0]["ok"]
    with pytest.raises(PermissionError):
        application.handle({"op": "state"}, "alice", 1)
    with pytest.raises(ValueError):
        application.handle({"op": "join", "player_id": "alice", "seat": 1}, None, 3)
    with pytest.raises(PermissionError):
        application.handle({"op": "join", "player_id": "bob", "seat": 1}, "alice", 2)
    application.detach(None, 1)
    application.detach("missing", 1)
    application.detach("alice", 2)
    with pytest.raises(PermissionError):
        application.handle({"op": "state"}, "alice", 2)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"op": "join", "player_id": "", "seat": 0, "stack": 1000},
        {"op": "join", "player_id": "a", "seat": True, "stack": 1000},
        {"op": "join", "player_id": "a", "seat": 0, "stack": 99},
        {"op": "join", "player_id": "a", "seat": 0, "stack": 10001},
        {"op": "events", "cursor": -1},
        {"op": "events", "cursor": False},
    ],
)
def test_bad_protocol_inputs_do_not_mutate_native_state(
    application: PokerApplication, payload: dict[str, object]
) -> None:
    before = application.game.state_version, application.game.event_cursor
    with pytest.raises(ValueError):
        application.handle(payload, None, 1)
    assert (application.game.state_version, application.game.event_cursor) == before


def admin_request(**changes: object) -> dict[str, object]:
    return {
        "op": "admin_create_game",
        "admin_token": "synthetic-test-only",
        "small_blind": 5,
        "big_blind": 10,
        "minimum_players": 2,
        "maximum_players": 6,
        "auto_start_delay": 1,
        "minimum_buy_in": 10,
        "maximum_buy_in": 5000,
        "seed": 7,
        **changes,
    }


@pytest.mark.parametrize("invalid", [math.nan, math.inf, -math.inf, True, "1", 0])
def test_admin_timeout_must_be_finite(invalid: object) -> None:
    app = PokerApplication(ServerConfig(admin_token="synthetic-test-only"))
    with pytest.raises(ValueError):
        app.handle(admin_request(auto_start_delay=invalid), None, 1)


def test_invalid_native_configuration_leaves_admin_settings_atomic() -> None:
    app = PokerApplication(ServerConfig(admin_token="synthetic-test-only"))
    before_game = app.game
    before = app.minimum_players, app.maximum_players, app.auto_start_delay, app.minimum_buy_in
    with pytest.raises((PokerError, ValueError)):
        app.handle(admin_request(maximum_players=23), None, 1)
    assert app.game is before_game
    assert (
        app.minimum_players,
        app.maximum_players,
        app.auto_start_delay,
        app.minimum_buy_in,
    ) == before
    assert app.handle(admin_request(), None, 1)[0]["admin"] is True
    assert app.maximum_players == 6 and app.minimum_buy_in == 10
    join(app, "alice", 0, 2)
    with pytest.raises(ValueError):
        app.handle(admin_request(), None, 1)


def test_admin_and_private_capabilities_are_explicit(application: PokerApplication) -> None:
    with pytest.raises(PermissionError):
        application.handle(admin_request(), None, 1)
    app = PokerApplication(ServerConfig(admin_token="synthetic-test-only"))
    with pytest.raises(PermissionError):
        app.handle(admin_request(admin_token="wrong"), None, 1)
    for request in (
        {"op": "leave"},
        {"op": "action", "action": "check"},
        {"op": "private_state"},
    ):
        with pytest.raises(PermissionError):
            app.handle(request, None, 1)
    join(app, "alice", 0, 1)
    with pytest.raises(PermissionError):
        app.handle({"op": "private_state"}, "alice", 1)


def test_timeout_policy_uses_native_legal_actions(
    application: PokerApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [100.0]
    monkeypatch.setattr("pokerdemo.game.service.time.monotonic", lambda: now[0])
    application.service_actions()
    join(application, "alice", 0, 1)
    join(application, "bob", 1, 2)
    application.service_actions()
    now[0] = 161
    application.service_actions()
    assert application.game.phase.value == "betting"
    before = application.game.state_version
    application.service_actions()
    assert application.game.state_version == before
    now[0] += application.action_timeout + 1
    application.service_actions()
    assert application.game.state_version > before
    assert application.game.phase.value == "finished"


def test_wire_projection_is_frozen_and_validated(application: PokerApplication) -> None:
    state = application.handle({"op": "state"}, None, 1)[0]["state"]
    parsed = TableState.from_dict(state)
    assert parsed.model_dump()["players"] == []
    with pytest.raises(ValidationError):
        parsed.version = 123
    assert LegalState.from_dict(None) is None
    legal = LegalState.from_dict(
        {
            "types": ["raise_to"],
            "call": None,
            "bet_to": None,
            "raise_to": {"minimum": 200, "maximum": 1000},
        }
    )
    assert legal is not None and legal.raise_to is not None
    assert (legal.raise_to.minimum, legal.raise_to.maximum) == (200, 1000)
    for value in (None, [], {1: "invalid-key"}):
        with pytest.raises(ValueError):
            TableState.from_dict(value)
    assert isinstance(state, dict)
    for key, value in [("version", True), ("board", [1]), ("players", {}), ("phase", 1)]:
        with pytest.raises(ValueError):
            TableState.from_dict({**state, key: value})


def test_public_dashboard_never_contains_private_player_cards(
    application: PokerApplication,
) -> None:
    join(application, "alice", 0, 1)
    join(application, "bob", 1, 2)
    application.handle({"op": "start"}, "alice", 1)
    view, connected, activity, result = application.dashboard_snapshot()
    assert all(player.cards is None for player in view.players)
    assert connected == {"alice", "bob"}
    assert result == ""
    assert activity[-1] == "HAND 1 started"
    application.detach("bob", 2)
    _, connected, activity, _ = application.dashboard_snapshot()
    assert "bob" not in connected
    assert "DISCONNECTED bob" in activity[-1]


def test_polling_does_not_extend_native_turn_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    monkeypatch.setattr("pokerdemo.game.service.time.monotonic", lambda: now[0])
    application = PokerApplication(ServerConfig(seed=42, action_timeout=10))
    join(application, "alice", 0, 1)
    join(application, "bob", 1, 2)
    application.handle({"op": "start"}, "alice", 1)
    application.service_actions()
    before = application.game.state_version
    for _ in range(9):
        now[0] += 1
        application.handle({"op": "state"}, "alice", 1)
        application.handle({"op": "state"}, "bob", 2)
        application.service_actions()
    assert application.game.state_version == before
    now[0] += 2
    application.service_actions()
    assert application.game.state_version > before
    assert application.game.phase.value == "finished"


def test_finished_example_hand_can_start_next_native_hand(application: PokerApplication) -> None:
    join(application, "alice", 0, 1)
    join(application, "bob", 1, 2)
    application.handle({"op": "start"}, "alice", 1)
    actor = application.game.current_player
    connection = 1 if actor == "alice" else 2
    application.handle({"op": "action", "action": "fold"}, actor, connection)
    assert application.game.phase.value == "finished"
    application.handle({"op": "start"}, "bob", 2)
    assert application.game.public_view().hand_id == 2
    assert application.game.phase.value == "betting"
