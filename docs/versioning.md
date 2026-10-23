# Versions

`[project].version` in `pyproject.toml` owns PokerDemo's application version,
currently `0.1.0.dev0`. `pokerDemo --version` reads installed distribution metadata.
PokerEngine's `2.0.0.dev2` wheel and the inherited template pins are independent.

This transfer creates a development application boundary, not a new stable engine
release. Existing engine commands need a migration link when removed from its active
dev tree; the public Python library API is unchanged by this repository.

Review CLI, configuration, runtime and wire compatibility before later version changes.
Do not move published tags or infer acceptance from a version number.
