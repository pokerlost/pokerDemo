# Architecture

PokerDemo is one in-memory table with a WebSocket boundary, a public operator console
and independent player consoles. The installed PokerEngine public API owns all
poker rules and state transitions.

## Component ownership

| Package | Owns |
| --- | --- |
| `cli.py`, `settings.py` | Thin Typer commands and lazy Pydantic Settings loading |
| `game/service.py` | Engine calls, player sessions, timeouts and recipient-specific projections |
| `protocol/models.py` | Strict Pydantic commands, replies, snapshots and state models |
| `network/server.py` | WebSocket lifecycle, message dispatch and snapshot publication |
| `network/client.py` | One reader, correlated replies and the latest pushed snapshot |
| `console/` | Textual server/player apps and shared Rich tables |
| `runtime/session.py` | Three processes in a workspace-owned tmux session |
| `runtime/recording.py` | Scripted inputs, real PTY capture and provenance receipt |
| `runtime/render.py` | agg replay, GIF and five exact replay frames |
| `runtime/tools.py` | Explicit executable discovery and verified local fallback tools |

There is no engine source copy, private adapter import or path injection. Native
engine development and tests belong to PokerEngine. The consoles render data and
send commands; they do not calculate poker rules.

## A command through the system

1. A console opens `/ws`, validates `hello` and joins a seat.
2. It subscribes to public state or its own permitted private view.
3. Textual input becomes a typed request with a correlation ID.
4. The server validates the envelope, then the game service checks the bound player
   and delegates the operation to PokerEngine.
5. A reply resolves that command. Changed snapshots are pushed to subscribers,
   including updates caused by another player or a timeout.

The server dashboard reads a public projection directly from the game service.
Its `start` and `settle` commands use the same engine lifecycle. An application lock
serializes table access; WebSocket reception and UI rendering remain separate.
See [the protocol](protocol.md) for wire limits and disconnect behavior.

## Privacy boundary

The public dashboard and recording observer use `public_view()`. A private
subscription requires explicit server enablement and a joined connection; its
snapshot comes from `player_view(bound_player_id)`. Each recipient gets a separate
projection. Synthetic recording clients show their own hands by default, so the
combined local GIF contains both hands; `record --hide-cards` uses public clients.
Trusted `server_view()` and opponent hole cards are never wire operations.

Results expose payout recipients, amounts and rake. Activity contains bounded public
descriptions and rejection categories, without complete request payloads or private
views. Synthetic player IDs are seat identifiers, not authenticated identities.

## Process and recording lifecycle

Manual play uses `.local/demo/tmux.sock`. Recording uses
`.local/demo-record/tmux.sock`, so capturing a scenario does not replace or stop a
manual game. Each owns only its `pokerdemo` session; unrelated tmux sessions are
outside this application's lifecycle. Paths are checked for ownership, type and
symlinks before runtime operations.

The recorder drives the same player consoles through tmux. It preserves real PTY
bytes and timing in asciicast v2, then agg replays that stream. The GIF uses a 1 fps
cap; five PNGs come from exact stable timestamps in the same cast. These are terminal
replay frames, not desktop screenshots or reconstructed game images.

The schema 2 receipt declares public server/observer views and either public or
own-player synthetic clients. Rendering rejects unsupported private-view policies;
legacy schema 1 remains valid only for public captures. The receipt records source
SHA, installed package/wheel checks, tools,
timestamps, scenario observations and file hashes. Capture and rendering have
separate completion flags. Failure and completion clean up the recording session.
All output stays beneath `.local/recordings/<run-id>/`.

[Demo guide](demo.md) · [Client](client.md) · [Project contract](../PROJECT.md) ·
[Transfer provenance](migration-source.json)
