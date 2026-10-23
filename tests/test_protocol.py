"""Exercise the reference TCP adapter against the authoritative native engine."""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import pty
import select
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

import pytest
from pokerengine import PokerError

from pokerdemo.client import (
    LegalState,
    ServerError,
    TableState,
    TCPTransport,
    console_request,
)
from pokerdemo.server import PokerApplication, PokerTCPServer, ServerConfig


@pytest.fixture
def application() -> PokerApplication:
    return PokerApplication(ServerConfig(seed=42, auto_start_delay=60), allow_private=True)


@contextlib.contextmanager
def serving(application: PokerApplication) -> Iterator[tuple[str, int]]:
    with PokerTCPServer(("127.0.0.1", 0), application) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield server.server_address
        finally:
            server.shutdown()
            thread.join(timeout=2)
            assert not thread.is_alive()


def join(application: PokerApplication, name: str, seat: int, connection: int) -> str:
    response, identity = application.handle(
        {"op": "join", "player_id": name, "seat": seat, "stack": 1000}, None, connection
    )
    assert response["ok"] is True and identity == name
    return name


def test_actual_tcp_roundtrip_privacy_legal_and_recovery(application: PokerApplication) -> None:
    with (
        serving(application) as address,
        contextlib.closing(TCPTransport(*address)) as alice,
        contextlib.closing(TCPTransport(*address)) as bob,
    ):
        alice.request({"op": "join", "player_id": "alice", "seat": 0, "stack": 1000})
        bob.request({"op": "join", "player_id": "bob", "seat": 1, "stack": 1000})
        ready = TableState.from_dict(alice.request({"op": "state"})["state"])
        assert ready.phase == "ready" and ready.auto_start_seconds is not None
        assert len(ready.available_seats) == 8
        assert set(ready.connected_players) == {"alice", "bob"}
        alice.request({"op": "start"})
        public = TableState.from_dict(bob.request({"op": "public_state"})["state"])
        private = TableState.from_dict(alice.request({"op": "private_state"})["state"])
        assert all(player.cards is None for player in public.players)
        assert len(next(p for p in private.players if p.player_id == "alice").cards or ()) == 2
        assert next(p for p in private.players if p.player_id == "bob").cards is None
        legal = LegalState.from_dict(alice.request({"op": "legal"})["legal_actions"])
        assert legal is not None and legal.call is not None
        current = alice if public.current_player == "alice" else bob
        other = bob if current is alice else alice
        with pytest.raises(ServerError, match="not this connection's turn"):
            other.request({"op": "action", "action": "call"})
        current.request({"op": "action", "action": "call"})
        events = alice.request({"op": "events", "cursor": 0})
        assert events["cursor"] and "HandStarted" in events["events"]
        with pytest.raises(ServerError, match="unknown operation"):
            alice.request({"op": "unsupported"})
        assert alice.request({"op": "state"})["ok"] is True
        assert bob.request({"op": "leave"})["away"] is True


@pytest.mark.parametrize("line", [b"not-json\n", b"[]\n", b"{}\n"])
def test_malformed_socket_request_is_local_and_next_request_works(
    application: PokerApplication, line: bytes
) -> None:
    with (
        serving(application) as address,
        socket.create_connection(address, timeout=2) as peer,
        peer.makefile("rwb", buffering=0) as stream,
    ):
        assert json.loads(stream.readline())["ok"] is True
        stream.write(line)
        assert json.loads(stream.readline())["ok"] is False
        stream.write(b'{"op":"state"}\n')
        assert json.loads(stream.readline())["ok"] is True


def test_oversize_socket_request_is_rejected_and_closed(application: PokerApplication) -> None:
    with (
        serving(application) as address,
        socket.create_connection(address, timeout=2) as peer,
        peer.makefile("rwb", buffering=0) as stream,
    ):
        stream.readline()
        stream.write(b"x" * (64 * 1024 + 1))
        assert json.loads(stream.readline())["error"] == "request is too large"
        assert stream.readline() == b""


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
    with pytest.raises(PermissionError, match="not bound"):
        application.handle({"op": "state"}, "alice", 1)
    with pytest.raises(ValueError, match="resume seat"):
        application.handle({"op": "join", "player_id": "alice", "seat": 1}, None, 3)
    with pytest.raises(PermissionError, match="only its bound"):
        application.handle({"op": "join", "player_id": "bob", "seat": 1}, "alice", 2)
    application.detach(None, 1)
    application.detach("missing", 1)
    application.detach("alice", 2)
    with pytest.raises(PermissionError, match="not bound"):
        application.handle({"op": "state"}, "alice", 2)


@pytest.mark.parametrize(
    "payload, error",
    [
        ({}, "op"),
        ({"op": "join", "player_id": "", "seat": 0, "stack": 1000}, "non-empty"),
        ({"op": "join", "player_id": "a", "seat": True, "stack": 1000}, "integer"),
        ({"op": "join", "player_id": "a", "seat": 0, "stack": 99}, "between"),
        ({"op": "join", "player_id": "a", "seat": 0, "stack": 10001}, "between"),
        ({"op": "events", "cursor": -1}, ">= 0"),
        ({"op": "events", "cursor": False}, "integer"),
    ],
)
def test_bad_protocol_inputs_do_not_mutate_native_state(
    application: PokerApplication, payload: dict[str, object], error: str
) -> None:
    before = application.game.state_version, application.game.event_cursor
    with pytest.raises(ValueError, match=error):
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
    with pytest.raises(ValueError, match="auto_start_delay"):
        app.handle(admin_request(auto_start_delay=invalid), None, 1)


def test_invalid_native_configuration_leaves_admin_settings_atomic() -> None:
    app = PokerApplication(ServerConfig(admin_token="synthetic-test-only"))
    before_game = app.game
    before = app.minimum_players, app.maximum_players, app.auto_start_delay, app.minimum_buy_in
    with pytest.raises(PokerError, match="invalid player limits"):
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
    with pytest.raises(ValueError, match="no connected players"):
        app.handle(admin_request(), None, 1)


def test_admin_and_private_capabilities_are_explicit(application: PokerApplication) -> None:
    with pytest.raises(PermissionError, match="disabled"):
        application.handle(admin_request(), None, 1)
    app = PokerApplication(ServerConfig(admin_token="synthetic-test-only"))
    with pytest.raises(PermissionError, match="invalid admin"):
        app.handle(admin_request(admin_token="wrong"), None, 1)
    for operation in ("leave", "action", "private_state"):
        with pytest.raises(PermissionError, match="join first"):
            app.handle({"op": operation}, None, 1)
    join(app, "alice", 0, 1)
    with pytest.raises(PermissionError, match="private state is disabled"):
        app.handle({"op": "private_state"}, "alice", 1)


def test_timeout_policy_uses_native_legal_actions(
    application: PokerApplication, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [100.0]
    monkeypatch.setattr("pokerdemo.server.time.monotonic", lambda: now[0])
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


def test_wire_projection_is_immutable_and_validated(application: PokerApplication) -> None:
    state = application.handle({"op": "state"}, None, 1)[0]["state"]
    parsed = TableState.from_dict(state)
    assert asdict(parsed)["players"] == ()
    assert LegalState.from_dict(None) is None
    assert LegalState.from_dict(
        {"types": ["check"], "call": None, "bet_to": None, "raise_to": None}
    )
    for value in (None, [], {1: "invalid-key"}):
        with pytest.raises(ServerError):
            TableState.from_dict(value)
    assert isinstance(state, dict)
    for key, value in [("version", True), ("board", [1]), ("players", {}), ("phase", 1)]:
        with pytest.raises(ServerError):
            TableState.from_dict({**state, key: value})


def test_real_terminal_client_setup_join_history_and_clean_exit(
    application: PokerApplication,
) -> None:
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
    output = bytearray()

    def rendered(marker: bytes) -> None:
        deadline = time.monotonic() + 30
        while marker not in output:
            assert time.monotonic() < deadline, (marker, bytes(output[-1000:]))
            readable, _, _ = select.select([master], [], [], 0.1)
            if readable:
                chunk = os.read(master, 65536)
                assert chunk, bytes(output[-1000:])
                output.extend(chunk)
        output.clear()

    with serving(application) as address:
        flags = ["-S"] if sys.flags.no_site else []
        process = subprocess.Popen(
            [sys.executable, *flags, "-m", "pokerdemo.client"],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "TERM": "xterm-256color", "LANG": "C.UTF-8"},
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
        )
        os.close(slave)
        try:
            rendered(b"127.0.0.1")
            os.write(master, b"\n")
            rendered(b"8765")
            os.write(master, str(address[1]).encode() + b"\n")
            rendered(b"Mode:")
            os.write(master, b"\n")
            rendered(b"private cards?")
            os.write(master, b"\n")
            rendered(b"Nickname")
            os.write(master, b"\nTerminalPlayer\n")
            rendered(b"Stack (")
            os.write(master, b"invalid\n1000\n")
            rendered(b"CHOOSE YOUR SEAT")
            os.write(master, b"\n")
            rendered(b"COMMUNITY BOARD")
            player = application.game.public_view().player("TerminalPlayer")
            assert player.stack == 1000 and player.cards is None
            os.write(master, b"s")
            rendered(b"ERROR:")
            assert application.game.phase.value == "waiting"
            os.write(master, b"h")
            rendered(b"HISTORY MODE")
            os.write(master, b"\n")
            rendered(b"COMMUNITY BOARD")
            os.write(master, b"q")
            deadline = time.monotonic() + 30
            while process.poll() is None and time.monotonic() < deadline:
                readable, _, _ = select.select([master], [], [], 0.1)
                if readable:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError:
                        break
            assert process.wait(timeout=5) == 0, bytes(output[-1000:])
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            os.close(master)


def test_two_real_clients_finish_hand_without_private_result_leak(
    application: PokerApplication,
) -> None:
    with (
        serving(application) as address,
        contextlib.closing(TCPTransport(*address)) as alice,
        contextlib.closing(TCPTransport(*address)) as bob,
    ):
        alice.request({"op": "join", "player_id": "alice", "seat": 0, "stack": 1000})
        bob.request({"op": "join", "player_id": "bob", "seat": 1, "stack": 1000})
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
        assert isinstance(result, dict)
        assert set(result) == {"payouts", "rake"}
        assert sum(payout.amount for payout in application.game.settlement.payouts) == 2000
        state = TableState.from_dict(bob.request({"op": "state"})["state"])
        assert state.phase == "finished" and all(p.cards is None for p in state.players)
        assert sum(player.stack for player in state.players) == 2000
        assert "HandFinished" in alice.request({"op": "events", "cursor": 0})["events"]
        alice.close()
        deadline = time.monotonic() + 2
        while "alice" in application._sessions and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "alice" not in application._sessions


@pytest.mark.parametrize(
    "command, expected",
    [
        ("state", {"op": "state"}),
        ("call", {"op": "action", "action": "call"}),
        ("raise 100", {"op": "action", "action": "raise_to", "total": 100}),
        ("settle", {"op": "settle"}),
        ("result", {"op": "result"}),
        ("", None),
    ],
)
def test_console_only_translates_input(command: str, expected: object) -> None:
    assert console_request(command) == expected


def test_console_rejects_unknown_input() -> None:
    with pytest.raises(ValueError):
        console_request("invented")


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
    monkeypatch.setattr("pokerdemo.server.time.monotonic", lambda: now[0])
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
