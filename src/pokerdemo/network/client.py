"""One reader dispatches replies; one bounded writer owns WebSocket sends."""

from __future__ import annotations

import json
import socket as sockets
import threading
import time
from concurrent.futures import Future
from contextlib import ExitStack, suppress
from queue import Empty, Full, Queue

from pydantic import ValidationError
from websockets.exceptions import ConnectionClosed, WebSocketException
from websockets.sync.client import connect

from pokerdemo.protocol.models import Hello, LegalState, Reply, Snapshot, TableState, server_message


class ServerError(RuntimeError):
    """A rejected command, unavailable server or invalid wire response."""


class WebSocketTransport:
    def __init__(self, host: str, port: int) -> None:
        self.url = f"ws://{host}:{port}/ws"
        self.host = host
        self.port = port
        self._lock = threading.RLock()
        self._pending: dict[int, Future[Reply]] = {}
        self._outbox: Queue[str] = Queue(maxsize=32)
        self._sequence = 0
        self.snapshot: Snapshot | None = None
        self.error: str | None = None
        self._closed = threading.Event()
        self._connection_context = ExitStack()
        self._socket = None
        self._reader: threading.Thread | None = None
        self._writer: threading.Thread | None = None
        self._closer: threading.Thread | None = None
        for attempt in range(20):
            try:
                self._socket = self._connection_context.enter_context(
                    connect(
                        self.url,
                        proxy=None,
                        compression=None,
                        open_timeout=1,
                        close_timeout=1,
                        max_size=64 * 1024,
                        max_queue=8,
                    )
                )
                break
            except (OSError, TimeoutError, WebSocketException) as error:
                if attempt == 19:
                    raise ServerError(f"Cannot connect to {self.url}") from error
                time.sleep(0.1)
        try:
            greeting = self._socket.recv(timeout=3)
            if not isinstance(greeting, str):
                raise ServerError("Server greeting must be JSON text")
            Hello.model_validate_json(greeting)
        except (ValidationError, ValueError, OSError, TimeoutError, WebSocketException) as error:
            self.close()
            raise ServerError("Server greeting is unavailable or invalid") from error
        except BaseException:
            self.close()
            raise
        self._writer = threading.Thread(target=self._send, name="poker-commands", daemon=True)
        self._reader = threading.Thread(target=self._receive, name="poker-replies", daemon=True)
        try:
            self._writer.start()
            self._reader.start()
        except BaseException:
            self.close()
            raise

    def _begin_close(self, reason: str) -> None:
        """Complete every waiter exactly once without joining any worker here."""
        with self._lock:
            if self._closer is not None:
                return
            self.error = self.error or reason
            self._closed.set()
            pending = list(self._pending.values())
            self._pending.clear()
            for future in pending:
                if not future.done():
                    future.set_exception(ServerError(self.error))
            self._closer = threading.Thread(
                target=self._close_socket, name="poker-close", daemon=True
            )
            self._closer.start()

    def _abort_socket(self) -> None:
        if self._socket is not None:
            with suppress(OSError):
                self._socket.socket.shutdown(sockets.SHUT_RDWR)
            with suppress(OSError):
                self._socket.socket.close()

    def _close_socket(self) -> None:
        if self._socket is None:
            return
        # A blocked sync send owns the library protocol mutex. Abort only this
        # connection after a grace period to release send, recv and close alike.
        deadline = threading.Timer(1.5, self._abort_socket)
        deadline.daemon = True
        deadline.start()
        try:
            with suppress(ConnectionClosed, OSError):
                self._connection_context.close()
        finally:
            deadline.cancel()
            deadline.join()

    def _send(self) -> None:
        try:
            while not self._closed.is_set():
                try:
                    message = self._outbox.get(timeout=0.1)
                except Empty:
                    continue
                if self._closed.is_set():
                    break
                self._socket.send(message)
        except ConnectionClosed, OSError, WebSocketException:
            self._begin_close("Connection to the server was lost")

    def _receive(self) -> None:
        reason = "Server disconnected"
        try:
            for raw in self._socket:
                if not isinstance(raw, str):
                    raise ServerError("Server sent a non-text message")
                message = server_message.validate_json(raw)
                with self._lock:
                    if self._closed.is_set():
                        break
                    if isinstance(message, Snapshot):
                        self.snapshot = message
                        continue
                    # Never guess which request an uncorrelated/duplicate reply belongs to.
                    future = self._pending.pop(message.id, None)
                    if future is None:
                        raise ServerError("Server sent an unexpected reply ID")
                    future.set_result(message)
        except ValidationError, ValueError, ServerError:
            reason = "Server sent an invalid or uncorrelated message"
        except ConnectionClosed, OSError, WebSocketException:
            reason = "Server disconnected"
        finally:
            self._begin_close(reason)

    def request(self, payload: dict[str, object]) -> dict[str, object]:
        with self._lock:
            if self._closed.is_set():
                raise ServerError(self.error or "Connection closed")
            if len(self._pending) >= 32:
                raise ServerError("Too many outstanding commands")
            self._sequence += 1
            request_id = self._sequence
            try:
                message = json.dumps(
                    {"id": request_id, "request": payload},
                    separators=(",", ":"),
                    allow_nan=False,
                )
            except (TypeError, ValueError) as error:
                raise ServerError("Command is not valid JSON") from error
            future: Future[Reply] = Future()
            self._pending[request_id] = future
            try:
                self._outbox.put_nowait(message)
            except Full as error:
                self._pending.pop(request_id, None)
                raise ServerError("Client command queue is full") from error
        try:
            # This bounds the entire request, including a stalled outgoing send.
            reply = future.result(timeout=5)
            if not reply.ok:
                raise ServerError(reply.error or "Command rejected")
            return reply.model_dump(exclude={"type", "id"})
        except TimeoutError as error:
            self.close()
            raise ServerError("Server response timed out") from error
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def subscribe(self, *, private: bool = False) -> None:
        self.request({"op": "subscribe", "private": private})

    def close(self) -> None:
        self._begin_close("Connection closed")
        closer = self._closer
        if closer is not None and closer is not threading.current_thread():
            closer.join(timeout=2)
            if closer.is_alive():
                self._abort_socket()
                closer.join(timeout=1)
        for worker in (self._writer, self._reader):
            if (
                worker is not None
                and worker.ident is not None
                and worker is not threading.current_thread()
            ):
                worker.join(timeout=2)
                if worker.is_alive():
                    self._abort_socket()
                    worker.join(timeout=1)
                if worker.is_alive():
                    raise ServerError("WebSocket worker did not stop")


class PokerProtocol:
    """Small command facade shared with recording; validation lives in wire models."""

    def __init__(self, transport: WebSocketTransport) -> None:
        self.transport = transport

    def join(self, player_id: str, seat: int, stack: int = 1000) -> dict[str, object]:
        return self.transport.request(
            {"op": "join", "player_id": player_id, "seat": seat, "stack": stack}
        )

    def start(self) -> dict[str, object]:
        return self.transport.request({"op": "start"})

    def settle(self) -> dict[str, object]:
        return self.transport.request({"op": "settle"})

    def result(self) -> dict[str, object]:
        return self.transport.request({"op": "result"})

    def state(self) -> TableState:
        return TableState.from_dict(self.transport.request({"op": "state"})["state"])

    def private_state(self) -> TableState:
        return TableState.from_dict(self.transport.request({"op": "private_state"})["state"])

    def legal(self) -> LegalState | None:
        return LegalState.from_dict(self.transport.request({"op": "legal"})["legal_actions"])

    def events(self, cursor: int) -> tuple[int, tuple[str, ...]]:
        response = self.transport.request({"op": "events", "cursor": cursor})
        return response["cursor"], tuple(response["events"])

    def action(self, name: str, total: int | None = None) -> dict[str, object]:
        payload = {"op": "action", "action": name}
        if total is not None:
            payload["total"] = total
        return self.transport.request(payload)
