"""Bounded WebSocket connections; the game service owns every game mutation."""

from __future__ import annotations

import json
import socket as sockets
import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field
from http import HTTPStatus
from queue import Empty, Full, Queue
from typing import Self

from pokerengine import PokerError
from pydantic import ValidationError
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response
from websockets.sync.server import ServerConnection, serve

from pokerdemo.game.service import PokerApplication
from pokerdemo.protocol.models import Command, Hello, Reply, Snapshot, Subscribe


@dataclass(eq=False)
class Peer:
    """One ordered, bounded outbox; no socket I/O runs on the game thread."""

    socket: ServerConnection
    identity: str | None = None
    subscribed: bool = False
    private: bool = False
    last_snapshot: str | None = None
    outbox: Queue[str] = field(default_factory=lambda: Queue(maxsize=32))
    _guard: threading.Lock = field(default_factory=threading.Lock)
    _closing: threading.Event = field(default_factory=threading.Event)
    _close_code: int = 1000
    _close_reason: str = ""
    _close_started: float | None = None
    _send_started: float | None = None
    _writer: threading.Thread | None = None

    def start(self) -> None:
        self._writer = threading.Thread(target=self._write, name="poker-outgoing", daemon=True)
        self._writer.start()

    def enqueue(self, message: str) -> bool:
        """Only a non-blocking queue operation may be called under the game lock."""
        with self._guard:
            if self._closing.is_set():
                return False
            try:
                self.outbox.put_nowait(message)
                return True
            except Full:
                self._mark_closing(1013, "Client is not consuming messages")
                return False

    def _mark_closing(self, code: int, reason: str) -> None:
        if not self._closing.is_set():
            self._close_code = code
            self._close_reason = reason
            self._close_started = time.monotonic()
            self._closing.set()

    def request_close(self, code: int = 1000, reason: str = "") -> None:
        with self._guard:
            self._mark_closing(code, reason)

    def abort(self) -> None:
        """Unblock this owned connection if a peer stalls inside sync sendall."""
        with suppress(OSError):
            self.socket.socket.shutdown(sockets.SHUT_RDWR)
        with suppress(OSError):
            self.socket.socket.close()

    def enforce_deadline(self) -> None:
        now = time.monotonic()
        with self._guard:
            slow_send = self._send_started is not None and now - self._send_started > 2
            slow_close = self._close_started is not None and now - self._close_started > 1.5
            if slow_send:
                self._mark_closing(1013, "Client is not consuming messages")
        if slow_send or slow_close:
            self.abort()

    def finish(self) -> None:
        self.request_close()
        writer = self._writer
        if writer is None or writer is threading.current_thread():
            return
        writer.join(timeout=1.5)
        if writer.is_alive():
            self.abort()
            writer.join(timeout=1.5)
        if writer.is_alive():
            raise RuntimeError("WebSocket outgoing worker did not stop")

    def _write(self) -> None:
        try:
            while not self._closing.is_set():
                try:
                    message = self.outbox.get(timeout=0.1)
                except Empty:
                    continue
                with self._guard:
                    if self._closing.is_set():
                        break
                    self._send_started = time.monotonic()
                try:
                    self.socket.send(message)
                finally:
                    with self._guard:
                        self._send_started = None
        except ConnectionClosed, OSError:
            pass
        finally:
            self.request_close()
            with suppress(ConnectionClosed, OSError):
                self.socket.close(self._close_code, self._close_reason)


class PokerWebSocketServer:
    """One in-memory table, public projections and connection-bound private views."""

    def __init__(self, address: tuple[str, int], application: PokerApplication) -> None:
        self.application = application
        self._peers: set[Peer] = set()
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.Lock()
        self._shutdown_lock = threading.Lock()
        self._stopping = threading.Event()
        self._started = threading.Event()
        self._timer: threading.Thread | None = None
        self._server = serve(
            self._handle,
            *address,
            origins=[None],
            compression=None,
            max_size=64 * 1024,
            max_queue=8,
            open_timeout=3,
            close_timeout=1,
            ping_interval=20,
            ping_timeout=20,
            process_request=self._route,
        )
        self.server_address = self._server.socket.getsockname()[:2]

    @staticmethod
    def _route(connection: ServerConnection, request: Request) -> Response | None:
        if request.path != "/ws":
            return connection.respond(HTTPStatus.NOT_FOUND, "Use /ws\n")
        return None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.shutdown()

    def serve_forever(self) -> None:
        with self._lifecycle_lock:
            if self._started.is_set():
                return
            self._started.set()
            self._timer = threading.Thread(target=self._tick, name="poker-timer", daemon=True)
            self._timer.start()
        try:
            self._server.serve_forever()
        finally:
            self._stopping.set()

    def shutdown(self) -> None:
        with self._shutdown_lock:
            self._stopping.set()
            with self._lifecycle_lock:
                if not self._started.is_set():
                    # websockets.shutdown waits for its accept loop to exit.
                    # If no loop was started, enter it once with the listener
                    # already closed so the library completes its own lifecycle.
                    self._started.set()
                    self._server.socket.close()
                    self._server.serve_forever()
            with self._lock:
                peers = list(self._peers)
                for peer in peers:
                    peer.request_close(1001, "Server stopped")
            # Finish our writers before the library waits for connection handlers.
            # No game lock is held while closing sockets or joining threads.
            for peer in peers:
                peer.finish()
            self._server.shutdown()
            if self._timer is not None and self._timer is not threading.current_thread():
                self._timer.join(timeout=3)
                if self._timer.is_alive():
                    raise RuntimeError("WebSocket timer did not stop")

    def _handle(self, socket: ServerConnection) -> None:
        peer = Peer(socket)
        peer.start()
        with self._lock:
            if self._stopping.is_set():
                peer.request_close(1001, "Server stopped")
            else:
                self._peers.add(peer)
                peer.enqueue(Hello(private_views=self.application.allow_private).model_dump_json())
        try:
            for raw in socket:
                if not isinstance(raw, str):
                    peer.request_close(1003, "Text JSON messages required")
                    break
                request_id = None
                try:
                    decoded = json.loads(raw)
                    if isinstance(decoded, dict):
                        candidate = decoded.get("id")
                        if type(candidate) is int and candidate >= 1:
                            request_id = candidate
                    command = Command.model_validate(decoded)
                except ValidationError, ValueError, RecursionError:
                    with self._lock:
                        self.application.note_rejection(peer.identity, "invalid_request")
                        peer.enqueue(
                            Reply(
                                id=request_id,
                                ok=False,
                                code="invalid_request",
                                error="Invalid command fields or JSON",
                            ).model_dump_json()
                        )
                        self._publish_locked()
                    continue
                with self._lock:
                    try:
                        if isinstance(command.request, Subscribe):
                            self.application.handle(
                                {"op": "private_state" if command.request.private else "state"},
                                peer.identity,
                                id(peer),
                            )
                            peer.private = command.request.private
                            peer.subscribed = True
                            peer.last_snapshot = None
                            result = {"ok": True, "subscribed": True}
                        else:
                            result, peer.identity = self.application.handle(
                                command.request.model_dump(exclude_none=True),
                                peer.identity,
                                id(peer),
                            )
                            if peer.identity is None:
                                peer.private = False
                        reply = Reply(id=command.id, **result)
                    except (ValueError, PermissionError, PokerError) as error:
                        self.application.note_rejection(peer.identity, type(error).__name__)
                        reply = Reply(id=command.id, ok=False, code="rejected", error=str(error))
                    peer.enqueue(reply.model_dump_json())
                    self._publish_locked()
        except ConnectionClosed, OSError:
            pass
        finally:
            with self._lock:
                self._peers.discard(peer)
                self.application.detach(peer.identity, id(peer))
                self._publish_locked()
            peer.finish()

    def _publish_locked(self) -> None:
        # A private projection is constructed separately for each bound connection.
        # Enqueue under the mutation lock preserves order without network I/O.
        for peer in self._peers:
            if not peer.subscribed:
                continue
            try:
                data = self.application.snapshot(peer.identity, id(peer), private=peer.private)
                message = Snapshot(**data).model_dump_json()
                if message != peer.last_snapshot and peer.enqueue(message):
                    peer.last_snapshot = message
            except PermissionError:
                peer.request_close(1008, "Player session replaced")

    def publish(self) -> None:
        with self._lock:
            self._publish_locked()

    def _tick(self) -> None:
        while not self._stopping.wait(0.1):
            with self._lock:
                self.application.service_actions()
                self._publish_locked()
                peers = list(self._peers)
            for peer in peers:
                peer.enforce_deadline()
