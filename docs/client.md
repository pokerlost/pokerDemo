# PokerEngine terminal client

`poker_client.py` uses curses for an automatically refreshed table and an editable
command line. Transport, wire validation, input and rendering stay in Python;
poker rules and legal ranges come from the server's native C++ engine.

## Start

```bash
.venv/bin/python -m pokerdemo.client --player-id alice --seat 0 --private
```

The server must already be running on loopback. `--host` and `--port` select its
endpoint. An explicit player ID skips the setup wizard; without it, the existing
interactive setup remains available. `--console` selects the retained line client.

The [canonical demo guide](demo.md#play) owns the key table, three-process
commands, next-hand lifecycle, tmux controls and capture procedure.

## Privacy and connection lifetime

`--private` requests only the connection-bound player's `player_view()` and requires
the server's explicit loopback private mode. The default is public-only; local
recordings use that mode. Opponent cards and trusted `server_view()` never enter the
client payload. Result messages contain only payout recipients, amounts and rake.

The client keeps polling while waiting or typing; a rejected command leaves the
connection usable. A lost connection is displayed explicitly. `Q` or `:quit` closes
its own socket; the server marks the player away and applies its documented native
check/fold timeout policy. ASCII suit codes avoid a card-font dependency.

> **Warning:** The example has no TLS or production authentication. IDs are synthetic seat identifiers, not credentials. Use loopback only.

See the [server protocol](protocol.md) and [architecture](architecture.md).
