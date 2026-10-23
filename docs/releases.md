# Release boundary

PokerDemo currently supports development use of an owner-supplied private engine
wheel. The source can be published on `main` while the application remains a
development candidate. No automatic test, build, release, upload or deployment
workflow is configured.

## Source publication

Prepare source on `dev`, review it and complete the agreed checks before an
explicitly authorized fast-forward promotion of the same commit to `main`.
Source publication creates no package release, tag, registry upload or deployment,
and does not stand in for tests that were not run.

## Before a stable public release

- Make the compatible engine dependency available through an approved channel.
- Review repository history, licenses and dependency/asset redistribution rights.
- Perform separately authorized tests, typing and distribution acceptance against
  the exact candidate, recording actual outcomes and unavailable checks.
- Decide version, promotion and tags explicitly, preserving existing history and
  branch protection.

Local recordings, diagnostics and dependency artifacts stay excluded from Git,
distributions and releases. Preserve MIT source notices and
[migration provenance](migration-source.json).

[Development](development.md) · [Versions](versioning.md) · [Demo guide](demo.md)
