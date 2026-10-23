# PokerDemo

Read the selected [baseRules engineering policy](https://github.com/serrja/baseRules/blob/bafcb63ed94b9769a0ca098e5db4b33c55dbc1b9/docs/engineering.md),
then the selected [Pokerlost policy](https://github.com/pokerlost/projectRules/blob/904c65a3daab88802fb5e414cdaccc7ad2b555a8/docs/project.md),
then [the local contract](PROJECT.md) and [architecture](docs/architecture.md).
Pinned links are reading sources, not loaders or permission grants.

This repository owns the loopback demonstration: transport, curses interfaces,
three-process tmux lifecycle and local recordings. PokerEngine's installed public
Python API owns all rules, actions, transitions and settlement. Never import its
private adapter, alter sys.path/PYTHONPATH or require a neighboring checkout.

Use the inherited Typer/Pydantic foundation; keep command callbacks thin and
configuration lazy. Preserve MIT source notices and transfer provenance. Keep
recordings, logs, screenshots and dependency artifacts in ignored local storage;
never add them to Git, CI artifacts, releases or public documentation.

Work only in dev. Run make lint before an explicitly authorized dev push. Tests,
coverage, type checks, builds and CI are manual. A narrowly authorized recording
is not full acceptance. Never overwrite tags, mutate main/test, discard unrelated
changes, broaden grants or stop processes outside this demo's own tmux socket.
