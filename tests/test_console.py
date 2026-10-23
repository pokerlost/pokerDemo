"""Check actual Textual input and layout at the user's smaller terminal size."""

import asyncio
import threading

from textual.events import Key
from textual.widgets import Footer, Input, Static

from pokerdemo.console.client import PlayerConsole
from pokerdemo.console.server import ServerConsole
from pokerdemo.game.service import PokerApplication, ServerConfig
from pokerdemo.network.client import WebSocketTransport
from pokerdemo.network.server import PokerWebSocketServer


def test_player_hotkeys_command_input_and_private_live_view() -> None:
    game = PokerApplication(ServerConfig(seed=42, auto_start_delay=3600), allow_private=True)
    with PokerWebSocketServer(("127.0.0.1", 0), game) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        bob = WebSocketTransport(*server.server_address)
        try:
            bob.request({"op": "join", "player_id": "bob", "seat": 1, "stack": 1000})

            async def exercise() -> None:
                app = PlayerConsole(*server.server_address, "alice", 0, 1000, True)
                async with app.run_test(size=(69, 19)) as pilot:
                    for _ in range(100):
                        await pilot.pause(0.05)
                        if app.transport and app.transport.snapshot:
                            break
                    assert app.transport and app.transport.snapshot
                    assert app.focused is None
                    await pilot.press("s")
                    for _ in range(100):
                        await pilot.pause(0.05)
                        if app.transport.snapshot.state.phase == "betting":
                            break
                    state = app.transport.snapshot.state
                    assert state.phase == "betting"
                    assert next(p for p in state.players if p.player_id == "alice").cards
                    assert next(p for p in state.players if p.player_id == "bob").cards is None
                    before = game.game.state_version, game.game.event_cursor
                    await pilot.press("colon")
                    assert isinstance(app.focused, Input)
                    await pilot.press(*"bet invalid", "enter")
                    await pilot.pause(0.2)
                    assert "REJECTED [bet invalid]" in str(
                        app.query_one("#feedback", Static).render()
                    )
                    assert (game.game.state_version, game.game.event_cursor) == before
                    assert app.focused is None
                    await pilot.press("colon")
                    command = app.query_one(Input)
                    assert command.content_region.height >= 1
                    assert command.region.bottom <= app.query_one(Footer).region.y
                    await pilot.press("escape")
                    app.query_one("#feedback", Static).update("")
                    # One PTY read may contain the focus key and the whole command.
                    # Those letters must never fire table hotkeys before focus settles.
                    for character in ":raise invalid":
                        key = (
                            "colon"
                            if character == ":"
                            else "space"
                            if character == " "
                            else character
                        )
                        app.post_message(Key(key, character))
                    app.post_message(Key("enter", "\r"))
                    await pilot.pause(0.3)
                    assert "REJECTED [raise invalid]" in str(
                        app.query_one("#feedback", Static).render()
                    )
                    assert (game.game.state_version, game.game.event_cursor) == before

            asyncio.run(exercise())
        finally:
            bob.close()
            server.shutdown()
            worker.join(timeout=3)
            assert not worker.is_alive()


def test_server_console_operator_commands_fit_small_pane() -> None:
    game = PokerApplication(ServerConfig(seed=42, auto_start_delay=3600), allow_private=True)
    for name, seat in (("alice", 0), ("bob", 1)):
        game.handle({"op": "join", "player_id": name, "seat": seat, "stack": 1000}, None, seat + 1)
    with PokerWebSocketServer(("127.0.0.1", 0), game) as server:

        async def exercise() -> None:
            app = ServerConsole(server)
            async with app.run_test(size=(141, 11)) as pilot:
                await pilot.press(*"status", "enter")
                assert "2 players connected" in str(app.query_one("#feedback", Static).render())
                await pilot.press(*"start", "enter")
                await pilot.pause(0.2)
                assert game.game.phase.value == "betting"
                assert "YOUR CARDS" not in str(app.query_one("#summary", Static).render())
                command = app.query_one(Input)
                assert command.content_region.height >= 1
                assert command.region.bottom <= app.query_one(Footer).region.y

        asyncio.run(exercise())
