# Versions

`[project].version` in `pyproject.toml` owns PokerDemo's application version,
currently `0.2.0.dev0`. `pokerDemo --version` reads installed distribution metadata.
PokerEngine's pinned `2.0.0.dev2` wheel has its own independent version.

## WebSocket revision

The `0.2.0.dev0` revision replaces raw newline-delimited TCP with WebSocket protocol 1,
strict envelopes and pushed snapshots. It also replaces curses with Textual consoles
and splits service, protocol, transport, presentation and runtime modules.

Legacy TCP clients must migrate their transport and envelopes. The former flat
modules remain as thin compatibility wrappers; new implementation belongs in the
named subpackages. The supported app entry point is `pokerDemo` (or
`python -m pokerdemo`); its `server` and `client` subcommands own the console options.
PokerEngine's public Python API is unchanged by this application revision.

Protocol identity is carried by `hello.protocol`; review message compatibility
separately from package version. Publishing development source to `main` does not
remove the `.dev0` qualifier or establish a stable release.

Before later version changes, review CLI, settings, wire and runtime compatibility.
Do not move published tags or infer acceptance from a version number.

[Development](development.md) · [Protocol](protocol.md) · [Release boundary](releases.md)
