# Interactive local demo

## Prerequisites

Use CPython 3.14 on Linux x86-64 and the owner-supplied
`pokerengine-2.0.0.dev2-cp314-cp314-linux_x86_64.whl`.
Its required SHA256 is
`13a1a57f6c61860cf1c1b89d1a4db624eda8d8cad132bdf7d61f0e94ab798ee3`.
The build source is `b66af7c4a61715a16a2015c8e437cdd5207cc448`;
transferred application source is recorded in [the manifest](migration-source.json).

Run preparation once from this repository root:

```bash
python3.14 scripts/prepare.py --wheel /path/to/pokerengine-2.0.0.dev2-cp314-cp314-linux_x86_64.whl
make doctor
```

Preparation checks the supplied artifact and Python before copying it to ignored
`.local/wheels/` and invoking `uv sync --locked --no-editable`. It installs locked
runtime and developer dependencies, with no native build or system changes.

Use installed `tmux` and official asciinema `agg`. Discovery is explicit
`POKERDEMO_TMUX` / `POKERDEMO_AGG`, then PATH, then the existing checksum-verified
cache under `${XDG_CACHE_HOME:-$HOME/.cache}/codex_dev_team/tools/` (`tmux-3.6a`
and `agg-1.9.0`). The cache is an optional host fallback, never downloaded by the app.
A missing prerequisite fails with the relevant override. No fonts are installed;
`POKERDEMO_FONT_FAMILY` selects an available renderer font.

## Start and stop

```bash
make demo
make demo-attach
make demo-stop
```

The server is above two independent clients. Switch panes with `Ctrl+B`, then an
arrow. `Ctrl+B`, then `D` detaches; `demo-attach` returns. Only this workspace's
`.local/demo/tmux.sock` and `pokerdemo` session are affected by stop. Closing or
interrupting a foreground launch stops its own session; an ordinary detach keeps
it running. `DEMO_PYTHON=/path/to/python` explicitly selects another installed environment.

The same apps can be run in three terminals with an explicit common loopback port:

```bash
.venv/bin/python -m pokerdemo.server --dashboard --private --seed 42 --auto-start-delay 3600 --action-timeout 300
.venv/bin/python -m pokerdemo.client --player-id alice --seat 0 --private
.venv/bin/python -m pokerdemo.client --player-id bob --seat 1 --private
```

The default endpoint is `127.0.0.1:8765`; the launcher selects a free loopback port.
`--console` on a client keeps the line-command interface available.

## Play

Once both players connect, either presses `S` to start. Each client sees only its
own private cards and the engine's permitted table projection. The server monitor
always uses the public view and never displays hole cards. Card codes are ASCII:
`As`, `Kh`, `Qd`, `Jc`; the suit letters remain readable without card-symbol fonts.

| Action | Keys | Context | Note |
| --- | --- | --- | --- |
| Start / next hand | `S` / `N` | Ready or finished table | Native lifecycle validates the request. |
| Fold / check / call | `F` / `C` / `L` | Your turn | The displayed native legal actions decide availability. |
| All-in | `A` | Your turn with a legal bet/raise range | Sends the native range maximum; no separate poker rules. |
| Bet or raise | `:bet TOTAL` / `:raise TOTAL`, Enter | Your turn | Inclusive limits are shown; TOTAL is the final street commitment. |
| Settle showdown | `D` | Showdown | C++ calculates payouts; clients display only amounts and recipients. |
| Enter any command | `:` or Enter | Table | `state`, `legal`, `start`, `settle`, `result` also work. |
| Cancel input | Escape | Command line | The table continues refreshing while typing. |
| Exit | `Q` or `:quit` | Client | Disconnect is visible to the server and other player. |

The table polls while waiting and while entering a command. Out-of-turn commands
are rejected locally; the server independently enforces the connection's identity
and turn. Malformed amounts and native rejections display a message without
changing game state. A disconnected or timed-out current player is checked when
native legal actions permit it, otherwise folded. Polling does not renew the turn
deadline. Starting the next hand remains an explicit player action after settlement.

> **Warning:** This is a loopback-only synthetic demonstration without TLS or production authentication. Player IDs are not credentials; a local reconnect can take over the matching example seat. Do not use real money, tokens or personal data.

## Local recording

First lint, review, commit and push dev; reinstall changed application code explicitly
with `uv sync --locked --no-editable`. Then:

```bash
make demo-record
```

This one command runs the actual server and two public-only clients, enters commands
through tmux, captures its real PTY, replays it with agg and extracts four frames:

```text
.local/recordings/<run-id>/
  session.cast
  tmux.typescript
  demo.gif
  frames/01-connected.png
  frames/02-action.png
  frames/03-late-street.png
  frames/04-result.png
  recording.json
```

Additional plain pane captures preserve diagnostics. `POKERDEMO_OUTPUT_ROOT` may
select another directory beneath this workspace's `.local/recordings/`; each run
gets a unique ID. All artifacts remain ignored by Git and excluded from packages
and Docker contexts. No media are linked as public README images or uploaded.

The seeded synthetic scenario covers normal streets/showdown, a fold, all-in,
invalid amount/input, out-of-turn rejection and disconnect. The engine determines
legal actions and results. Receipt observations and completion flags show what
actually happened. Both recorded clients hide private cards; manual clients retain
their own permitted views. Frames are unchanged PTY replay frames, not desktop screenshots.

The recorder checks clean committed source, exact installed demo files, engine wheel
metadata/bytes and tool hashes before and after capture. Failure and completion stop
its own session. A successful run is narrow manual demonstration verification;
full tests, typing, build acceptance and CI remain separate.

[Protocol](protocol.md) · [Client](client.md) · [Architecture](architecture.md)
