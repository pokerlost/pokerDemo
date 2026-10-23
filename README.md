# PokerDemo

Local terminal demonstration of the installed PokerEngine Python API: one server,
two independent clients, manual play and reproducible local recording. The engine
owns poker rules and settlement; this repository owns transport and presentation.

## Prepare once

The supplied engine wheel targets CPython 3.14 on Linux x86-64. Obtain the exact
private artifact from its owner; it is not downloaded or built by demo commands.
Install uv, tmux and asciinema agg separately. This host can use the already verified
local tool cache as a fallback; see [prerequisites](docs/demo.md#prerequisites).

```bash
python3.14 scripts/prepare.py --wheel /path/to/pokerengine-2.0.0.dev2-cp314-cp314-linux_x86_64.whl
```

Preparation verifies SHA256 and Python compatibility before installing the locked
environment and this package non-editably. It does not build the native engine.

## Use

```bash
make demo          # manual server and two clients
make demo-record   # real scripted game, PTY capture, GIF and four frames
make demo-stop     # stop only this workspace's demo session
```

Recordings are private local files in `.local/recordings/<run-id>/`. Nothing is
uploaded. `make demo-record` prints the GIF, four PNG paths and provenance receipt.
Repeated recording does not install dependencies, build the engine or run tests.

Read the [demo guide](docs/demo.md), [protocol](docs/protocol.md),
[client](docs/client.md), [architecture](docs/architecture.md) and
[development workflow](docs/development.md). [Migration provenance](docs/migration-source.json)
records source commits, original file hashes and licenses. Historical engine
recordings remain in their original Git history and private backups.

`make lint` is the daily gate. Full tests, typing and release acceptance are separate
manual actions. This synthetic loopback demonstration has no production identity,
TLS or real-money integration. The pinned wheel requires private artifact access;
the repository is not yet a standalone public installation.

Code is [MIT licensed](LICENSE); preserve [third-party notices](THIRD_PARTY_NOTICES.md).
