# Architecture

`cli.py` uses Typer and lazy Pydantic Settings for explicit configuration. `session.py`
starts a server and two client processes in this workspace's dedicated tmux socket.
The launcher selects a loopback port and never depends on a neighboring repository.

`server.py` calls the installed engine's public API. It validates JSON requests,
binds player views to their connections and projects public monitoring data.
`client.py` owns transport, typed wire parsing, command input and curses rendering.
It obtains legal ranges from the server; it does not implement poker rules.

`session.py` also drives a separate seeded recording through the same client input.
The PTY recorder preserves real tmux bytes and timing as asciicast v2. `render.py`
uses official agg to replay the stream and Pillow to extract four unchanged frames.
The receipt records source SHA, exact installed wheel bytes, tool versions/hashes,
timestamps, commands, state observations and output hashes. Frames are terminal
replay images, not desktop screenshots.

`tools.py` discovers explicit executable overrides, then PATH, then one verified
existing local cache fallback. No runtime command installs or downloads anything.
`make demo-stop` addresses only `.local/demo/tmux.sock` and the `pokerdemo` session.
Recording failure and completion stop that session; unrelated tmux sockets remain
untouched. Detaching manual mode deliberately leaves its session available.

[Demo guide](demo.md) · [Protocol](protocol.md) · [Client](client.md) ·
[Transfer manifest](migration-source.json) · [Project contract](../PROJECT.md)
