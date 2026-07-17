# Release Contract

Hypergraph's package release boundary is human-controlled. Automation builds
and publishes artifacts, but it does not decide that a production release
should exist.

## Before and after

Before:

```bash
pip install git+https://github.com/gilad-rubin/hypergraph.git
```

After the first beta is published:

```bash
pip install hypergraph-ai
python -c 'import hypergraph; g = hypergraph.Graph([])'
```

`hypergraph-ai` is the distribution name. The import name remains
`hypergraph`.

## Versioning and maturity

The first beta is `0.2.0b1` and carries the
`Development Status :: 4 - Beta` classifier. Stable-beta APIs receive a
`DeprecationWarning` and at least one minor release of grace before removal.
Experimental surfaces may change without that grace period, with the change
recorded in the changelog.

Real package releases use tags shaped like `vX.Y.Z`, with an optional
prerelease suffix such as `v0.2.0b1`. A patch component is mandatory. The
historical `v1.0`, `v1.1`, and `v1.2` tags are milestones, not package
versions, so the release workflow rejects them.

## Human production trigger

Production publication starts only when a maintainer publishes a GitHub
Release whose tag matches `vX.Y.Z*`. A tag push by itself does not trigger the
workflow. The workflow builds the wheel and source distribution in an
unprivileged job, then the PyPI job downloads those exact artifacts and uses
Trusted Publishing through GitHub OIDC. No PyPI token is stored as a secret.

The maintainer must configure the `pypi` GitHub environment as a Trusted
Publisher on PyPI before the first production release. Publishing the GitHub
Release is explicit approval to upload that version.

## TestPyPI dry run

TestPyPI is separate from the production trigger. A maintainer manually runs
the `Release` workflow and enables `publish_testpypi`. The `testpypi`
environment must already be configured as a TestPyPI Trusted Publisher. A
manual run can publish only to TestPyPI; the production job is gated on the
`release.published` event.

Before using either index, the same local artifact checks are available:

```bash
uv build --clear
uv run python scripts/verify_distribution.py dist/*.whl dist/*.tar.gz
```

## Historical milestone tag migration

Remote tag history is maintainer-owned. After reviewing the three targets, a
maintainer can create annotated milestone tags and remove the old remote names
with this single compound command:

```bash
git tag -a milestone/1.0 'v1.0^{}' -m 'v1.0 Type Validation' && git tag -a milestone/1.1 'v1.1^{}' -m 'v1.1 Documentation' && git tag -a milestone/1.2 'v1.2^{}' -m 'v1.2 Test Coverage' && git push origin refs/tags/milestone/1.0 refs/tags/milestone/1.1 refs/tags/milestone/1.2 && git push origin --delete v1.0 v1.1 v1.2
```

The release-readiness implementation does not run this command or otherwise
rewrite remote tag history.

## Bad releases

Yank a bad release from the package index and publish a corrected version.
Never delete a published release. Yanking keeps the historical record while
preventing ordinary dependency resolution from selecting the bad artifact.
