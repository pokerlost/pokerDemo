"""Focused real-socket checks for backpressure, failed peers and owned cleanup."""

from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect
from websockets.sync.server import ServerConnection, serve

from pokerdemo.game.service import PokerApplication, ServerConfig
from pokerdemo.network.client import ServerError, WebSocketTransport
from pokerdemo.network.server import PokerWebSocketServer
from pokerdemo.protocol.models import Hello


def abort_socket(connection) -> None:
    """Force cleanup of a socket created by this test, even after an assertion."""
    with contextlib.suppress(OSError):
        connection.socket.shutdown(socket.SHUT_RDWR)
    with contextlib.suppress(OSError):
        connection.socket.close()


def wait_until(predicate: Callable[[], bool], timeout: float = 2) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "The expected connection cleanup did not finish"
        time.sleep(0.01)


def application_workers() -> set[int | None]:
    return {
        thread.ident
        for thread in threading.enumerate()
        if thread.name.startswith(("poker-replies", "poker-commands", "poker-close"))
    }


@contextlib.contextmanager
def unread_peer(raw_socket: socket.socket, address: tuple[str, int]):
    host, port = address
    with connect(
        f"ws://{host}:{port}/ws",
        sock=raw_socket,
        proxy=None,
        compression=None,
        max_queue=1,
        close_timeout=0.3,
        ping_interval=None,
    ) as peer:
        try:
            yield peer
        finally:
            # Abort before the context manager tries its graceful close: the
            # flooding thread may intentionally be stuck in sendall at this point.
            abort_socket(peer)


@contextlib.contextmanager
def fake_server(
    handler: Callable[[ServerConnection], None],
) -> Iterator[tuple[tuple[str, int], threading.Event]]:
    closed = threading.Event()

    def observed(connection: ServerConnection) -> None:
        try:
            handler(connection)
        except ConnectionClosed:
            pass
        finally:
            closed.set()

    server = serve(observed, "127.0.0.1", 0, compression=None, close_timeout=0.3, open_timeout=1)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server.socket.getsockname()[:2], closed
    finally:
        # These are all connections of this test's temporary fake server.
        for connection in server.connections:
            abort_socket(connection)
        server.shutdown()
        worker.join(timeout=2)
        assert not worker.is_alive()


@pytest.mark.parametrize(
    "hello",
    [
        "not-json",
        '{"type":"hello","protocol":999,"server":"pokerDemo","private_views":false}',
        b"binary-greeting",
    ],
)
def test_malformed_hello_closes_the_connection_without_client_workers(
    hello: str | bytes,
) -> None:
    def handler(peer: ServerConnection) -> None:
        peer.send(hello)
        peer.recv(timeout=5)

    with fake_server(handler) as (address, peer_closed):
        before = application_workers()
        with pytest.raises(ServerError):
            WebSocketTransport(*address)
        assert peer_closed.wait(timeout=2)
        wait_until(lambda: application_workers() <= before)


def test_missing_hello_times_out_and_releases_the_connection() -> None:
    def handler(peer: ServerConnection) -> None:
        peer.recv(timeout=5)

    with fake_server(handler) as (address, peer_closed):
        before = application_workers()
        started = time.monotonic()
        with pytest.raises(ServerError):
            WebSocketTransport(*address)
        assert time.monotonic() - started < 5
        assert peer_closed.wait(timeout=2)
        wait_until(lambda: application_workers() <= before)


@pytest.mark.parametrize("failure", ["malformed", "uncorrelated", "wrong_id", "disconnect"])
def test_pending_request_fails_promptly_when_server_reply_is_unusable(failure: str) -> None:
    def handler(peer: ServerConnection) -> None:
        peer.send(Hello(private_views=False).model_dump_json())
        command = json.loads(peer.recv(timeout=3))
        if failure == "malformed":
            peer.send("not-json")
        elif failure == "disconnect":
            peer.close(1011, "Synthetic server failure")
            return
        else:
            peer.send(
                json.dumps(
                    {
                        "type": "reply",
                        "id": None if failure == "uncorrelated" else command["id"] + 100,
                        "ok": False,
                        "error": "Synthetic rejection",
                    }
                )
            )
        peer.recv(timeout=3)

    with fake_server(handler) as (address, peer_closed):
        before = application_workers()
        with contextlib.closing(WebSocketTransport(*address)) as client:
            started = time.monotonic()
            with pytest.raises(ServerError):
                client.request({"op": "state"})
            assert time.monotonic() - started < 2
        assert peer_closed.wait(timeout=2)
        wait_until(lambda: application_workers() <= before)


def test_duplicate_reply_cannot_complete_a_future_twice() -> None:
    def handler(peer: ServerConnection) -> None:
        peer.send(Hello(private_views=False).model_dump_json())
        command = json.loads(peer.recv(timeout=3))
        reply = json.dumps({"type": "reply", "id": command["id"], "ok": True})
        peer.send(reply)
        peer.send(reply)
        peer.recv(timeout=3)

    with fake_server(handler) as (address, peer_closed):
        before = application_workers()
        with contextlib.closing(WebSocketTransport(*address)) as client:
            assert client.request({"op": "state"})["ok"] is True
            wait_until(lambda: client.error is not None)
            with pytest.raises(ServerError):
                client.request({"op": "state"})
        assert peer_closed.wait(timeout=2)
        wait_until(lambda: application_workers() <= before)


def test_unstarted_server_shutdown_is_idempotent_and_bounded() -> None:
    # A child process gives the regression a hard deadline: the broken library
    # lifecycle otherwise waits forever for a listener loop that never started.
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pokerdemo.game.service import PokerApplication, ServerConfig\n"
            "from pokerdemo.network.server import PokerWebSocketServer\n"
            "with PokerWebSocketServer(('127.0.0.1', 0), "
            "PokerApplication(ServerConfig(seed=42))) as server:\n"
            "    server.shutdown()\n"
            "    server.shutdown()\n",
        ],
        capture_output=True,
        text=True,
        timeout=4,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_slow_reader_does_not_hold_game_lock_or_delay_shutdown() -> None:
    application = PokerApplication(ServerConfig(seed=42, auto_start_delay=60))
    server = PokerWebSocketServer(("127.0.0.1", 0), application)
    listener = threading.Thread(target=server.serve_forever, daemon=True)
    listener.start()
    stop_flood = threading.Event()
    flooding = threading.Event()
    pressure_closed = threading.Event()
    sender: threading.Thread | None = None
    shutdown_worker: threading.Thread | None = None
    shutdown_errors: list[BaseException] = []
    slow = None
    healthy = None
    raw_socket = socket.socket()
    # Force a real small advertised receive window before the WS handshake.
    raw_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
    raw_socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
    raw_socket.settimeout(2)
    raw_socket.connect(server.server_address)
    try:
        with unread_peer(raw_socket, server.server_address) as slow:
            assert json.loads(slow.recv(timeout=2))["type"] == "hello"
            healthy = WebSocketTransport(*server.server_address)

            def flood_without_reading() -> None:
                try:
                    for request_id in range(1, 50_001):
                        if stop_flood.is_set():
                            break
                        slow.send(json.dumps({"id": request_id, "request": {"op": "state"}}))
                        if request_id == 64:
                            flooding.set()
                except ConnectionClosed, OSError:
                    flooding.set()
                    pressure_closed.set()

            sender = threading.Thread(target=flood_without_reading, daemon=True)
            sender.start()
            assert flooding.wait(timeout=2)
            pressure_deadline = time.monotonic() + 5
            while not pressure_closed.is_set():
                assert time.monotonic() < pressure_deadline, "No receiving backpressure observed"
                probe_started = time.monotonic()
                assert healthy.request({"op": "state"})["ok"] is True
                assert time.monotonic() - probe_started < 2
                time.sleep(0.02)
            started = time.monotonic()
            response = healthy.request(
                {"op": "join", "player_id": "alice", "seat": 0, "stack": 1000}
            )
            assert response["ok"] is True
            assert time.monotonic() - started < 3
            assert healthy.request({"op": "state"})["state"]["connected_players"] == ["alice"]

            def shutdown() -> None:
                try:
                    server.shutdown()
                except BaseException as error:
                    shutdown_errors.append(error)

            shutdown_worker = threading.Thread(target=shutdown, daemon=True)
            shutdown_worker.start()
            shutdown_worker.join(timeout=4)
            if shutdown_worker.is_alive():
                # Unblock only our own stalled peer before reporting the failure.
                abort_socket(slow)
                shutdown_worker.join(timeout=2)
                pytest.fail("A stalled receiver prevented bounded server shutdown")
            assert not shutdown_errors
            with pytest.raises(ServerError):
                healthy.request({"op": "state"})
            stop_flood.set()
            abort_socket(slow)
            sender.join(timeout=2)
            assert not sender.is_alive()
    finally:
        stop_flood.set()
        if slow is not None:
            abort_socket(slow)
        with contextlib.suppress(OSError):
            raw_socket.close()
        if healthy is not None:
            healthy.close()
        if sender is not None:
            sender.join(timeout=2)
        if shutdown_worker is not None:
            shutdown_worker.join(timeout=2)
        server.shutdown()
        listener.join(timeout=2)
        assert not listener.is_alive()
