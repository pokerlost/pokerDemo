# Contributing

Work in `dev` and follow [the local contract](PROJECT.md). Preserve the pinned source
notices and provenance when changing transferred files. Use the installed public
PokerEngine API; keep game rules and production platform code outside this demo.

Prepare once using the [README](README.md), then run `make format` and `make lint`.
Review staged paths and secrets before an authorized commit and `git push origin dev`.
Local recordings, logs, wheels and private backup files must never be staged or
uploaded. Full tests, typing, CI, release and promotion are manual separate actions.

A change report distinguishes implemented code, lint, an observed demo scenario and
checks that were not run. See [development](docs/development.md).
