# WebSocket protocol

PokerDemo serves one table at `ws://127.0.0.1:8765/ws` by default. The launcher chooses
a free loopback port for its own session. The maintained `websockets` library owns
WebSocket framing, handshakes, ping/pong and connection closure.

## Connection and messages

The server accepts CLI connections without an `Origin` header. Other origins and
paths are not supported. Each application message is one UTF-8 JSON text message;
there is no newline-delimited TCP mode.

On connection, the server sends:

```json
{"type":"hello","protocol":1,"server":"pokerDemo","private_views":true}
```

`private_views` describes server capability. The actual value depends on whether
the server was started with `--private`.

Clients send a positive integer `id` and a discriminated `request`:

```json
{"id":1,"request":{"op":"join","player_id":"alice","seat":0,"stack":1000}}
```

The server returns `type: "reply"` with the same `id`, `ok` and operation-specific
data. Rejections contain `code` and `error`. Invalid JSON or envelope fields may
have `id: null` because no trustworthy request ID could be parsed. Nullable fields
and empty `events` can be present in replies; the models define the complete shape.

Pydantic models use strict types and reject unknown fields. Join IDs are 1–32 printable
ASCII characters; seats are integers from 0 to 9. The game validates table capacity,
buy-in limits and phase independently.

## Subscribe to changes

After joining, a player can request its own private updates:

```json
{"id":2,"request":{"op":"subscribe","private":true}}
```

A public observer can subscribe with `private: false` without joining. Private
subscription requires both server permission and a connection-bound player.
Successful subscription is acknowledged, then `type: "snapshot"` messages carry
`state`, `legal_actions`, `result` and recent public `activity`. Updates are pushed
when their projection changes; clients do not need to poll table state.

Replies and snapshots share the connection. A receiver must dispatch by `type`
and correlate replies by `id`, rather than assuming the next message answers its
last command. Each private snapshot is built for its recipient.

## Operations

| Operation | Additional request fields | Behavior |
| --- | --- | --- |
| `join` | `player_id`, `seat`, optional `stack` | Bind this connection to one player. |
| `subscribe` | Optional `private` | Enable pushed public or bound-player snapshots. |
| `leave` | None | Mark the player away and clear this connection's binding. |
| `start`, `settle` | None | Run the native hand lifecycle; a joined player is required. |
| `action` | `action`, optional `total` | Apply an action for the bound player on their turn. |
| `state`, `public_state` | None | Read the public table projection. |
| `private_state` | None | Read only the bound player's enabled private projection. |
| `legal` | None | Read native legal actions and inclusive total ranges. |
| `result` | None | Read payouts and rake, without showdown hands. |
| `events` | Non-negative `cursor` | Read event names and the next cursor, without event payloads. |

Valid actions are `fold`, `check`, `call`, `bet_to`, `raise_to` and `all_in`.
Only `bet_to` and `raise_to` accept and require `total`:

```json
{"id":3,"request":{"op":"start"}}
```

```json
{"id":4,"request":{"op":"action","action":"raise_to","total":200}}
```

`total` is the final commitment for the street, not an added amount. `all_in` uses
the maximum native legal bet/raise range. PokerEngine decides legality and payouts.
Commands rejected by validation do not apply a game mutation.

The retained `admin_create_game` contract is disabled by the ordinary CLI server.
An explicitly configured application requires its admin token, no connected players
and a permitted lifecycle before replacing a lobby. It is not production authentication.

## Limits and connection lifetime

Incoming messages are limited to 64 KiB with a bounded incoming frame queue.
Binary messages close with code `1003`; oversized messages close with `1009`.
Each connection has a bounded outgoing queue and its own writer. A stalled receiver
is closed without holding the table lock or delaying other players. Compression is
disabled, and handshake, heartbeat and close timeouts are explicit.
Normal command validation errors produce a reply and leave the connection usable.

Disconnect or `leave` marks a player away. A watchdog checks a disconnected or
timed-out current player when the native legal set permits it, otherwise folds.
The deadline follows the active turn; reading state does not extend it.

A reconnect with the same player ID and seat replaces that synthetic local session.
The previous connection loses control and private access. The server shuts down its
connections with `1001` when stopped. There is no durable table recovery.

This loopback demonstration has no TLS or production authentication. Use synthetic
players and chips only. Protocol 1 replaces the earlier raw TCP contract; old clients
must migrate their transport and envelope.

[Client](client.md) · [Architecture](architecture.md) · [Demo guide](demo.md)
