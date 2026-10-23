# Development

Develop on `dev` from this repository's exact root. Prepare the locked environment
using [the README](../README.md), then keep changes within the owning package:
game service, wire contract, network transport, console or runtime tooling.

## Local change cycle

```bash
make format
make lint
```

Lint performs Ruff checks and format verification. It does not install dependencies,
start a game, run tests or build the engine. Review the full diff, staged paths and
secrets before an authorized commit and explicit `git push origin dev`.
Keep runtime recordings, logs, wheels and private backups out of staged files.
The README preview in `docs/assets/` is a separately authorized publication of one
reviewed synthetic GIF. Match its hash to the completed local receipt, preserve the
source commit in its provenance record, and do not copy casts or diagnostic frames.

After changing application code, explicitly reinstall it before a recorded run:

```bash
uv sync --locked --no-editable --reinstall-package pokerdemo
make demo-record
```

Recording requires clean committed source already pushed to the current `dev` or
`main` branch. It compares installed package files against that commit. Reinstallation
builds the small Python demo package; PokerEngine is installed from the pinned wheel
and is not rebuilt here. Runtime recording itself performs no installation.

## Checks and evidence

`make test` and `make typecheck` are available as separate manual checks. Their
presence does not imply they have run. Native engine/API tests remain with
PokerEngine; this application's tests belong to its own service, wire and CLI
boundaries. A recording is narrow integration evidence for its observed scenario.

Keep reports under ignored local storage. Distinguish code implemented, lint result,
observed manual behavior and checks not run. Do not describe a successful GIF as
full-suite, performance or production acceptance.

## Promotion

Main receives reviewed source through an explicitly authorized promotion. Complete
dev review and the agreed verification before promoting the same commit with a
fast-forward only update. Preserve branch protections, `test` and existing tags;
do not force-push or create a release implicitly.

Each promotion requires its own decision. Main publication does not change the
development version or imply stable release acceptance.
No automatic hosted CI, artifact upload or deployment is configured here.

[Architecture](architecture.md) · [Version policy](versioning.md) ·
[Release boundary](releases.md) · [Transfer provenance](migration-source.json)
