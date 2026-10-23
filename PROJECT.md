# PokerDemo contract

PokerDemo owns synthetic loopback WebSocket transport, a public server dashboard,
connection-bound player clients, a dedicated tmux lifecycle and local PTY recording.
The installed `pokerengine==2.0.0.dev2` public Python API owns game rules, validation,
legal ranges, transitions and settlement. Native sources are not copied here.

The supported artifact is CPython 3.14/Linux x86-64. The owner supplies its immutable
wheel; preparation checks the pinned SHA256. No neighboring checkout, editable engine,
private adapter import, sys.path override or implicit tool installation is supported.

Manual mode preserves each player's own permitted view. Recording is a separate
synthetic seeded scenario showing each client's own permitted hand by default;
`record --hide-cards` selects public-only clients. The server monitor and recorder
observer always remain public. All generated media, logs,
wheel artifacts and runtime state stay under ignored `.local/`; recordings belong
under `.local/recordings/<run-id>/` and are excluded from distribution packaging.

This is a development demonstration without production authentication, encryption,
payments or deployment. New work and authorized pushes use `dev`; full acceptance,
promotion and publication require separate decisions. No automatic CI is configured.
