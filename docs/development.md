# Development

Use `dev`. Prepare the pinned environment once as described in the [README](../README.md).
After changes run `make format` and `make lint`, review the complete diff and staged
paths for secrets/local artifacts, then make an explicitly authorized dev commit/push.
Main, test and existing tags are unchanged. An ordinary push runs no hosted CI.

Before recording, push the committed source and explicitly reinstall changed demo
code with `uv sync --locked --no-editable`. This builds only the small Python demo
package. The native engine comes from the supplied wheel; it is never rebuilt here.
The recorder compares installed demo files with the committed source and refuses a
mismatch. `make demo-record` itself only launches and records the installed code.

`make test` and `make typecheck` are manual acceptance commands, not part of lint.
The transferred protocol tests remain available in `tests/test_protocol.py`; native
engine and public API tests stay with PokerEngine. Adapted foundation tests cover the
actual settings/CLI contract. Their presence does not imply they were run.

Keep receipts and diagnostics in ignored local storage. A successful demo run proves
that recorded scenario only; it does not establish full-suite, performance or production
acceptance. See [versions](versioning.md), [releases](releases.md) and
[migration source](migration-source.json).
