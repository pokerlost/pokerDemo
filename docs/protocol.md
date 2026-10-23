# PokerEngine TCP server

`poker_server.py` owns one authoritative `PokerGame` behind a newline-delimited JSON
protocol. Its curses dashboard displays only public table state, connections,
actions, rejections and payout amounts; it never reads or displays trusted cards.

## Start

```bash
.venv/bin/python -m pokerdemo.server --dashboard --private --seed 42
```

`--dashboard` uses explicit CLI options with the public monitor. `--headless` uses
the same options without curses. Omitting both retains the setup wizard. Every
CLI mode requires loopback. See the [demo guide](demo.md) for the complete
three-process command, timing options, key table and recording.

## Protocol

Each request and response is one JSON object per line. Typical requests are:

```json
{"op":"join","player_id":"alice","seat":0,"stack":1000}
{"op":"start"}
{"op":"legal"}
{"op":"action","action":"raise_to","total":200}
{"op":"state"}
{"op":"events","cursor":0}
```

`state` and `public_state` use `public_view()` and include occupied/available seats,
connection status, connected-player IDs and buy-in limits. `private_state` requires the enabled private
mode and a bound connection, then uses only `player_view(bound_player_id)`.
`legal` returns native action types and inclusive bet/raise totals. `all_in` is an
input alias for a native bet/raise range maximum, not a separate rules engine.

Joined players can request `start` and `settle`. Native lifecycle validation rejects
incorrect phases. `result` includes payout recipients, amounts and rake, never
showdown hands. `events` exposes event names and cursor without private event data.
The protocol has no operation exposing `server_view()`.

## Disconnects and diagnostics

Closing a socket or sending `leave` marks that session away. The watchdog checks
when native legal actions permit it, otherwise folds an inactive current player.
The action deadline follows the native turn/state version; polling does not extend
it. A local reconnect with the same player ID and seat takes over the example
session. The dashboard copies its public snapshot under the application lock.

Diagnostics retain bounded public action descriptions and rejection categories;
request bodies, admin tokens and private views are not logged. The optional existing
admin operation creates a new lobby only with no connected players and an allowed
native lifecycle. It is demonstration administration, not production auth.

> **Warning:** This loopback-only protocol has no TLS or production authentication. Use only synthetic players and chips.

See [privacy boundaries](architecture.md) and the [client](client.md).
