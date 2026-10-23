"""Exercise the player menu against the real loopback WebSocket service."""

import asyncio
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from textual.pilot import Pilot
from textual.widgets import Button, Footer, Input, Static

from pokerdemo.console.cards import CardsView
from pokerdemo.console.client import PlayerConsole
from pokerdemo.game.service import PokerApplication, ServerConfig
from pokerdemo.network.client import WebSocketTransport
from pokerdemo.network.server import PokerWebSocketServer


@contextmanager
def local_table() -> Iterator[tuple[PokerApplication, PokerWebSocketServer, WebSocketTransport]]:
    game = PokerApplication(
        ServerConfig(seed=42, auto_start_delay=3600, action_timeout=300), allow_private=True
    )
    with PokerWebSocketServer(("127.0.0.1", 0), game) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        bob = WebSocketTransport(*server.server_address)
        try:
            bob.request({"op": "join", "player_id": "bob", "seat": 1, "stack": 1000})
            yield game, server, bob
        finally:
            bob.close()
            server.shutdown()
            worker.join(timeout=3)
            assert not worker.is_alive()


async def until(pilot: Pilot, predicate: Callable[[], bool]) -> None:
    for _ in range(100):
        if predicate():
            await pilot.pause(0.1)
            return
        await pilot.pause(0.03)
    raise AssertionError("The expected pushed state did not reach the console")


async def connected(pilot: Pilot, app: PlayerConsole) -> None:
    await until(
        pilot,
        lambda: bool(
            app.transport
            and app.transport.snapshot
            and len(app.transport.snapshot.state.players) == 2
        ),
    )


async def start_on_alice(pilot: Pilot, app: PlayerConsole, bob: WebSocketTransport) -> None:
    await connected(pilot, app)
    await pilot.click("#start")
    assert app.transport is not None
    await until(pilot, lambda: app.transport.snapshot.state.phase == "betting")
    if app.transport.snapshot.state.current_player != "alice":
        legal = app.transport.snapshot.legal_actions
        assert legal is not None
        action = "call" if "call" in legal.types else "check"
        await asyncio.to_thread(bob.request, {"op": "action", "action": action})
        await until(pilot, lambda: app.transport.snapshot.state.current_player == "alice")


def test_start_button_displays_private_cards_and_fits_small_client() -> None:
    with local_table() as (_, server, _):

        async def exercise() -> None:
            app = PlayerConsole(*server.server_address, "alice", 0, 1000, True)
            async with app.run_test(size=(69, 25)) as pilot:
                await connected(pilot, app)
                assert not app.query_one("#command", Input).display
                start = app.query_one("#start", Button)
                assert not start.disabled
                await pilot.click("#start")
                assert app.transport is not None
                await until(pilot, lambda: app.transport.snapshot.state.phase == "betting")
                state = app.transport.snapshot.state
                own = next(player for player in state.players if player.player_id == "alice")
                opponent = next(player for player in state.players if player.player_id == "bob")
                assert len(own.cards or []) == 2
                assert opponent.cards is None
                cards = app.query_one("#cards", CardsView)
                rendered = str(cards.content)
                assert "YOUR HAND" in rendered and "COMMUNITY CARDS" in rendered
                assert "Private" in rendered
                for card in own.cards or []:
                    suit = {"s": "♠", "h": "♥", "d": "♦", "c": "♣"}.get(card[-1].lower(), card[-1])
                    assert suit in rendered
                assert cards.region.height >= 7
                assert 0 <= cards.region.x < cards.region.right <= app.size.width
                assert cards.region.bottom <= app.size.height
                assert app.query_one("#wager", Button).region.bottom <= app.size.height
                await pilot.press("colon")
                assert app.query_one("#command", Input).display
                assert app.focused is app.query_one("#command", Input)
                await pilot.press("escape")
                assert not app.query_one("#command", Input).display

        asyncio.run(exercise())


def test_waiting_player_cannot_use_action_menu() -> None:
    with local_table() as (game, server, bob):

        async def exercise() -> None:
            app = PlayerConsole(*server.server_address, "alice", 0, 1000, True)
            async with app.run_test(size=(69, 25)) as pilot:
                await start_on_alice(pilot, app, bob)
                assert app.transport is not None
                legal = app.transport.snapshot.legal_actions
                assert legal is not None
                action = "call" if "call" in legal.types else "check"
                await pilot.click("#" + action)
                await until(pilot, lambda: app.transport.snapshot.state.current_player != "alice")
                before = game.game.state_version, game.game.event_cursor
                for selector in ("#fold", "#check", "#call", "#wager", "#all-in"):
                    button = app.query_one(selector, Button)
                    assert button.disabled or not button.display
                # Waiting-state hotkeys must be as safe as the disabled buttons.
                await pilot.press("c", "l", "f", "a")
                await pilot.pause(0.2)
                assert (game.game.state_version, game.game.event_cursor) == before

        asyncio.run(exercise())


def test_wager_dialog_uses_native_minimum_and_places_valid_raise() -> None:
    with local_table() as (game, server, bob):

        async def exercise() -> None:
            app = PlayerConsole(*server.server_address, "alice", 0, 1000, True)
            async with app.run_test(size=(69, 25)) as pilot:
                await start_on_alice(pilot, app, bob)
                assert app.transport is not None
                legal = app.transport.snapshot.legal_actions
                assert legal is not None and legal.raise_to is not None
                minimum = legal.raise_to.minimum
                before = game.game.state_version
                await pilot.click("#wager")
                await pilot.click("#minimum")
                assert int(app.screen.query_one("#amount", Input).value) == minimum
                await pilot.click("#confirm")
                await until(pilot, lambda: app.transport.snapshot.state.version > before)
                own = next(
                    player
                    for player in app.transport.snapshot.state.players
                    if player.player_id == "alice"
                )
                assert own.street_committed == minimum
                assert app.transport.snapshot.state.current_player == "bob"
                assert not app.screen.query("#amount")

        asyncio.run(exercise())


def test_invalid_wager_stays_in_dialog_without_engine_mutation() -> None:
    with local_table() as (game, server, bob):

        async def exercise() -> None:
            app = PlayerConsole(*server.server_address, "alice", 0, 1000, True)
            async with app.run_test(size=(69, 25)) as pilot:
                await start_on_alice(pilot, app, bob)
                assert app.transport is not None
                legal = app.transport.snapshot.legal_actions
                assert legal is not None
                bounds = legal.raise_to or legal.bet_to
                assert bounds is not None
                before = game.game.state_version, game.game.event_cursor
                await pilot.click("#wager")
                amount = app.screen.query_one("#amount", Input)
                amount.value = str(bounds.minimum - 1)
                await pilot.click("#confirm")
                await pilot.pause(0.2)
                assert app.screen.query_one("#amount", Input).value == amount.value
                assert (game.game.state_version, game.game.event_cursor) == before
                await pilot.click("#cancel")
                assert not app.screen.query("#amount")

        asyncio.run(exercise())


def test_canceled_and_stale_wagers_cannot_place_an_extra_action() -> None:
    with local_table() as (game, server, bob):

        async def exercise() -> None:
            app = PlayerConsole(*server.server_address, "alice", 0, 1000, True)
            async with app.run_test(size=(69, 25)) as pilot:
                await start_on_alice(pilot, app, bob)
                before = game.game.state_version, game.game.event_cursor
                await pilot.click("#wager")
                await pilot.click("#cancel")
                assert not app.screen.query("#amount")
                assert (game.game.state_version, game.game.event_cursor) == before
                await pilot.click("#wager")
                await pilot.click("#minimum")
                assert app.transport is not None
                legal = app.transport.snapshot.legal_actions
                assert legal is not None
                action = "call" if "call" in legal.types else "check"
                # A real legal command advances the table while the modal retains
                # its opening version. Confirmation must not send a second action.
                await asyncio.to_thread(app.transport.request, {"op": "action", "action": action})
                await until(pilot, lambda: app.transport.snapshot.state.version != before[0])
                after = game.game.state_version, game.game.event_cursor
                await pilot.click("#confirm")
                await pilot.pause(0.2)
                assert (game.game.state_version, game.game.event_cursor) == after
                feedback = str(app.query_one("#feedback", Static).content).lower()
                assert "changed" in feedback or "stale" in feedback

        asyncio.run(exercise())


def test_public_client_deals_concealed_backs_without_hole_card_faces() -> None:
    with local_table() as (_, server, _):

        async def exercise() -> None:
            app = PlayerConsole(*server.server_address, "alice", 0, 1000, False)
            async with app.run_test(size=(69, 25)) as pilot:
                await connected(pilot, app)
                await pilot.click("#start")
                assert app.transport is not None
                await until(pilot, lambda: app.transport.snapshot.state.phase == "betting")
                state = app.transport.snapshot.state
                assert not state.board
                assert all(player.cards is None for player in state.players)
                cards = str(app.query_one("#cards", CardsView).content)
                assert "hidden" in cards.lower()
                assert "░" in cards and "◆" in cards
                assert all(suit not in cards for suit in ("♠", "♥", "♦", "♣"))

        asyncio.run(exercise())


def test_disconnect_disables_menu_without_needing_a_new_snapshot() -> None:
    with local_table() as (game, server, bob):

        async def exercise() -> None:
            app = PlayerConsole(*server.server_address, "alice", 0, 1000, True)
            async with app.run_test(size=(69, 25)) as pilot:
                await start_on_alice(pilot, app, bob)
                assert app.transport is not None
                assert not app.query_one("#wager", Button).disabled
                snapshot = app.transport.snapshot
                # Closing the actual client connection retains its latest snapshot.
                # The UI must react to transport.error independently of state updates.
                await asyncio.to_thread(app.transport.close)
                await until(
                    pilot,
                    lambda: (
                        bool(app.transport.error)
                        and all(button.disabled for button in app.query("#actions Button"))
                    ),
                )
                assert app.transport.snapshot is snapshot
                for selector in (
                    "#start",
                    "#settle",
                    "#fold",
                    "#check",
                    "#call",
                    "#wager",
                    "#all-in",
                ):
                    assert app.query_one(selector, Button).disabled
                assert "exit" in str(app.query_one("#feedback", Static).content).lower()
                # Let the server apply its own disconnect check/fold before
                # measuring whether disabled UI controls add another operation.
                await until(pilot, lambda: game.game.current_player != "alice")
                before = game.game.state_version, game.game.event_cursor
                await pilot.press("s", "c", "l", "f", "a", "r")
                await pilot.pause(0.2)
                assert (game.game.state_version, game.game.event_cursor) == before
                assert not app.screen.query("#amount")

        asyncio.run(exercise())


def test_full_layout_supports_tab_enter_and_enter_to_confirm_wager() -> None:
    with local_table() as (game, server, bob):

        async def exercise() -> None:
            app = PlayerConsole(*server.server_address, "alice", 0, 1000, True)
            async with app.run_test(size=(79, 33)) as pilot:
                await connected(pilot, app)
                assert app.focused is None
                await pilot.press("tab")
                assert isinstance(app.focused, Button) and app.focused.id == "start"
                await pilot.press("enter")
                assert app.transport is not None
                await until(pilot, lambda: app.transport.snapshot.state.phase == "betting")
                assert not app.query_one("#command", Input).display
                if app.transport.snapshot.state.current_player != "alice":
                    legal = app.transport.snapshot.legal_actions
                    assert legal is not None
                    action = "call" if "call" in legal.types else "check"
                    await asyncio.to_thread(bob.request, {"op": "action", "action": action})
                    await until(
                        pilot, lambda: app.transport.snapshot.state.current_player == "alice"
                    )
                legal = app.transport.snapshot.legal_actions
                assert legal is not None
                bounds = legal.raise_to or legal.bet_to
                assert bounds is not None
                for _ in range(8):
                    if isinstance(app.focused, Button) and app.focused.id == "wager":
                        break
                    await pilot.press("tab")
                assert isinstance(app.focused, Button) and app.focused.id == "wager"
                await pilot.press("enter")
                amount = app.screen.query_one("#amount", Input)
                assert app.focused is amount
                amount.value = str(bounds.minimum)
                before = game.game.state_version
                await pilot.press("enter")
                await until(pilot, lambda: app.transport.snapshot.state.version > before)
                assert not app.screen.query("#amount")
                own = next(
                    player
                    for player in app.transport.snapshot.state.players
                    if player.player_id == "alice"
                )
                assert own.street_committed == bounds.minimum
                footer = app.query_one(Footer)
                for selector in ("#cards", "#turn", "#actions", "#feedback"):
                    widget = app.query_one(selector)
                    assert widget.region.bottom <= footer.region.y
                await pilot.press("colon")
                command = app.query_one("#command", Input)
                assert command.display and app.focused is command
                assert command.content_region.height >= 1
                assert command.region.bottom <= footer.region.y
                await pilot.press("escape")
                assert not command.display

        asyncio.run(exercise())
