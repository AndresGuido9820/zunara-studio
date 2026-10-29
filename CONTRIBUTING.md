# Contributing

Open an issue for a bug or a proposed behavior change. Include the command, expected result,
actual result and a small synthetic reproduction. Do not attach account tokens or private footage.

Install with `uv sync --locked --extra youtube --group dev` and run the checks in the README.
Add a regression test with the fix it protects. Keep network access out of the normal test suite.

Before opening a PR, read [Git workflow](docs/git-workflow.md), [commit conventions](docs/commits.md)
and [PR review](docs/pull-requests.md). The GitHub PR template is the entry point. Use the short
description for docs/test-only changes, and the complete description for behavioral or contract changes.

The maintainer reviews changes on a best-effort basis. Contributions are licensed under MIT.
