# Player console

The Textual client shows a card table, current turn, player stacks and an action
menu. Rich draws cream card faces with red and dark suits, plus public player rows.
Poker rules and legal actions come from the server's installed native engine.

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

Click a legal action or select it with Tab and Enter. Actions unlock on your turn.
Bet and Raise open a small amount picker with the engine's inclusive legal range,
Min / half-pot / pot presets and an explicit Confirm button. The amount is the final
street commitment, including chips already committed. Escape cancels. A table change
while the picker is open invalidates its confirmation; choose from the updated menu.

The command field stays hidden until `:` (or Enter with no button selected).
Enter submits and returns to the table; Escape closes it. For example, `raise 200`
requests a final street commitment of 200. Rejections appear in the feedback line.
Hotkeys work outside text fields. The layout becomes compact below 30 rows; allow
at least 60 columns and 19 rows per player pane for the complete controls.

`Ctrl+Q` or the `quit` command exits this client and closes its connection. An
unexpected server disconnect is shown in the feedback line and disables the menu.
Start a new client to reconnect; there is no silent reconnect or local reconstruction of game state.

## Views

A private client receives only its connection-bound player's permitted view.
Opponent cards and trusted engine state are not client payloads. The server monitor
remains public. Recording uses the same private-recipient clients for the synthetic
Alice and Bob seats by default, so the combined local GIF displays both own hands.
`record --hide-cards` keeps hole cards concealed in the recording.

Cards use ordinary Unicode suit glyphs and ranks; no image assets or font installation
are needed. Empty board slots fill as the server advances streets. At settlement the
client retains only the cards it already saw, labelled Last hand, alongside payouts.
The next hand replaces them.
Player IDs are synthetic seat names, not credentials; a matching local reconnect
can take over a seat in this demonstration.

[Protocol](protocol.md) · [Architecture](architecture.md) · [Demo guide](demo.md)
