# Player console

The small Textual client shows the table, turn, legal ranges, recent public activity
and a command input. Rich renders the player rows. Poker rules and legal actions
come from the server's installed native engine.

## Run a client

With a server running on the default loopback endpoint:

```bash
.venv/bin/pokerDemo client --player-id alice --seat 0 --private
```

`--player-id` and `--seat` are required. `--host`, `--port` and `--stack` explicitly
select the endpoint and buy-in; the default is `127.0.0.1:8765` and 1000 synthetic
chips. The supplied CLI only accepts IPv4 loopback hosts. `--private` requires the
server's private mode. Without it, the client displays public state only.

There is no setup wizard or alternate curses client. For the usual two-player
layout, use `make demo`; the [demo guide](demo.md#play) owns the full key table.

## Input and updates

After connecting and joining, the client subscribes to updates. A background reader
validates replies and snapshots with Pydantic, correlates command IDs and retains the
latest snapshot. Textual refreshes its widgets without polling the server for state.
Updates continue while the user types.

Hotkeys work when the command input is not focused. Escape returns to that mode;
`:` or Enter focuses input. Enter submits a command and returns to hotkeys. For
example, type `raise 200` to request a final street commitment of 200. Rejections
appear in the feedback line; they do not terminate the UI.

`Ctrl+Q` or the `quit` command exits this client and closes its connection. An
unexpected server disconnect is shown in the feedback line. Start a new client to
reconnect; there is no silent reconnect or local reconstruction of game state.

## Views

A private client receives only its connection-bound player's permitted view.
Opponent cards and trusted engine state are not client payloads. The server monitor
remains public. Recording clients intentionally hide all private cards.

Cards use ASCII rank/suit codes such as `As`, `Kh`, `Qd`, `Jc`, so reading them does
not require a special card-symbol font. Results display payout amounts and recipients.
Player IDs are synthetic seat names, not credentials; a matching local reconnect
can take over a seat in this demonstration.

[Protocol](protocol.md) · [Architecture](architecture.md) · [Demo guide](demo.md)
