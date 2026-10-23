# Demo guide

Use manual mode to play both sides, or recording mode to run a reproducible scenario
and save a GIF. They are separate sessions with separate lifecycle state.

## Prerequisites

Use CPython 3.14 on Linux x86-64, `uv`, `tmux` and the owner-supplied
`pokerengine-2.0.0.dev2-cp314-cp314-linux_x86_64.whl`. Recording also needs official
asciinema `agg`. The required wheel SHA256 is:

```text
13a1a57f6c61860cf1c1b89d1a4db624eda8d8cad132bdf7d61f0e94ab798ee3
```

Its build source is `b66af7c4a61715a16a2015c8e437cdd5207cc448`.
[Transfer provenance](migration-source.json) records the original application source.

Run preparation from this repository root:

```bash
python3.14 scripts/prepare.py --wheel /path/to/pokerengine-2.0.0.dev2-cp314-cp314-linux_x86_64.whl
make doctor
```

Preparation checks the wheel name, checksum, ABI and current interpreter. It copies
the artifact to ignored `.local/wheels/`, then runs `uv sync --locked --no-editable --reinstall-package pokerdemo`.
The locked dependencies and this Python application are installed; the native
engine is not rebuilt. Runtime commands never install dependencies or tools.

Tool discovery checks `POKERDEMO_TMUX` / `POKERDEMO_AGG`, then PATH, then an optional
existing checksum-verified cache under
`${XDG_CACHE_HOME:-$HOME/.cache}/codex_dev_team/tools/`. The fallback is not downloaded.
Missing tools produce an error with the relevant override. `POKERDEMO_FONT_FAMILY`
selects an already installed renderer font; no fonts are installed by this project.

## Open the game

```bash
make demo
```

Use a terminal around 160 columns by 50 rows. The public server dashboard is above
Alice and Bob. The launcher selects a free loopback port and focuses Alice.
**Click Deal hand (or press `S`) in a player panel to start.**

Click a player panel to control it, or switch with `Ctrl+B`, then an arrow.
`Ctrl+B`, then `D` detaches and leaves the manual game running. Return or stop it from another terminal:

```bash
make demo-attach
make demo-stop
```

Stop addresses only this workspace's `.local/demo/tmux.sock` and `pokerdemo` session.
Other tmux sessions and the recording socket are separate. An interrupted launcher
cleans up its own session; an ordinary detach deliberately keeps it running.

For three independent terminals, use the same explicit port:

```bash
.venv/bin/pokerDemo server --private --seed 42 --port 8765
.venv/bin/pokerDemo client --player-id alice --seat 0 --private --port 8765
.venv/bin/pokerDemo client --player-id bob --seat 1 --private --port 8765
```

The server console accepts `status`, `start`, `settle` and `quit` followed by Enter.
Player actions belong to the player consoles. `server --no-dashboard` runs a
headless WebSocket listener instead. Defaults use `127.0.0.1:8765/ws`.

## Play

Each client shows your two cards, the five community-card positions, the pot,
player stacks and the current turn. Red suits are hearts and diamonds; dark suits
are spades and clubs. Each player receives only their own permitted hole cards.
The public server console never displays hole cards.

Choose an action with the mouse, or use **Tab** to select a button and **Enter** to
activate it. Unavailable actions are disabled. **Bet** and **Raise** open an amount
picker with **Min**, **½ pot** and **Pot** presets. The amount is the total for the
current street, including chips already committed. The server supplies the legal
range; the dialog checks your amount before confirmation. **Esc** cancels it.
**All-in** uses the maximum permitted bet or raise; when only a short-stack call is
available, use **Call**.

| Key | Action |
| --- | --- |
| `S` / `N` | Deal the first / next hand |
| `C` | Check |
| `L` | Call the displayed amount |
| `F` | Fold |
| `B` / `R` | Choose a bet / raise amount |
| `A` | Bet / raise to the permitted maximum |
| `D` | Settle showdown and show the result |
| `:` | Open the command field |
| `Esc` | Close the command field or cancel the amount picker |
| `Ctrl+Q` | Exit the selected console |

The optional command field accepts `start`, `check`, `call`, `fold`, `bet TOTAL`,
`raise TOTAL`, `all_in` and `settle`. Press **Enter** to submit. To stop the entire
manual demo, run `make demo-stop` from another terminal.

If the current player disconnects or times out, the server checks when the native
legal actions permit it, otherwise folds. Reading updates does not renew the action
deadline. Player IDs are local seat identifiers, not credentials.

## Record a game

```bash
make demo-record
```

**This is an unattended background run, not an interactive window.** It prints
three stages: play, render GIF, extract frames. Allow about 60 seconds for play and
roughly 20–40 seconds for GIF rendering, plus frame extraction; actual time depends
on the host. Wait for the final `Done` and output paths.

To open the finished GIF automatically:

```bash
make demo-record-open
```

The local viewer is launched only after successful recording and rendering. Both
commands use `.local/demo-record/tmux.sock`, independent of the manual game.

Each run creates:

```text
.local/recordings/<run-id>/
├── session.cast
├── tmux.typescript
├── demo.gif
├── frames/
│   ├── 01-connected.png
│   ├── 02-action.png
│   ├── 03-late-street.png
│   ├── 04-result.png
│   └── 05-wager-menu.png
└── recording.json
```

Plain pane captures may also be retained for diagnostics. Generated output remains
local, ignored by Git and excluded from packages and Docker contexts. The README's
separately reviewed preview is one deliberately published GIF, with provenance in
`docs/assets/demo.json`; source casts, diagnostic frames and logs are not copied.
`POKERDEMO_OUTPUT_ROOT` can choose a subdirectory beneath this workspace's `.local/recordings/`; each run
still gets its own ID. No output is uploaded.

The seeded scenario drives the actual server and client consoles through normal
streets, showdown, fold, all-in, rejected commands and disconnect. The first raise
opens the same amount picker used in manual play and confirms the native minimum.

By default, Alice and Bob each subscribe to their own permitted hand. The combined
local GIF therefore shows both synthetic hands; neither client receives the other's
private cards. The server dashboard and recording observer remain public. To record
concealed hole cards, use `.venv/bin/pokerDemo record --hide-cards` or
`POKERDEMO_RECORD_SHOW_CARDS=false make demo-record`. Explicit `--show-cards` overrides
that environment setting. Recording enables colors only for its own child processes;
it does not change the user's shell, fonts or terminal settings.

PokerEngine determines actions and results. The GIF replays the real PTY cast with a 1 fps cap.
Each PNG comes from an exact stable timestamp in the same cast using agg and lossless
Pillow conversion. They are replay frames, not desktop screenshots.

The receipt separates capture and rendering completion, records observations and
hashes the outputs. Source must be committed, clean and pushed on `dev` or `main`;
installed application files must match it. The engine wheel and tool identity are
checked as part of the run. Capture completion or failure stops the recording session.

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| Recording seems idle | Read the stage message; it runs in the background. Wait for `Done`, then open the printed GIF path. |
| A server remains after recording | A manual game may still be running independently. Use `make demo-attach` or `make demo-stop` for that session. |
| The table is waiting | Select Alice or Bob, press Escape, then `S`. |
| Hotkeys type letters | Press Escape to leave command input. |
| Demo already exists | Use `make demo-attach`, or stop that manual session before starting another. |
| Installed source differs | After reviewing and pushing changes, explicitly run `uv sync --locked --no-editable --reinstall-package pokerdemo`. |
| Wheel/tool check fails | Follow the named prerequisite error; demo commands never install or silently replace it. |

An observed recording verifies only that scenario. Full tests, typing, packaging and
CI remain separate. This is a synthetic loopback demo without TLS, production
authentication, payments or deployment.

[Client](client.md) · [Protocol](protocol.md) · [Architecture](architecture.md) ·
[Development](development.md)
