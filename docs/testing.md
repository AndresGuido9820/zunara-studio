# Test architecture

Tests run against the installed `src` package using pytest's importlib mode. CI never reads personal
media, authorizes OAuth or posts to a real account. Tests generate their own tiny fixtures.

| Layer | Directory | What it proves |
| --- | --- | --- |
| Unit | `tests/unit` | Input validation, queue state, hashes, private JSON permissions, topic policy at the CLI boundary, signal edge cases and caption escaping |
| Contract | `tests/contracts` | YouTube request shape, channel mismatch, previews, scheduling, success and interrupted uploads using a fake SDK |
| Integration | `tests/integration` | Installed CLI and real FFmpeg/ffprobe with synthetic media; no model downloads or Internet |

Tests describe behavior rather than reproducing every implementation statement. Negative cases
matter: path traversal and symlinks, changed media after approval, wrong channels, unknown upload
states, malformed queues and naive/past schedules must fail before remote writes.

```sh
uv run pytest tests/unit
uv run pytest tests/contracts
uv run pytest tests/integration
uv run pytest --cov=zunara --cov-report=term-missing
```

Coverage measures tested lines, not correctness. Acquisition and audio matching are optional stages
and excluded from the initial coverage denominator; they have explicit algorithm tests where possible.
Real Kick compatibility, model recognition accuracy and live OAuth require manual smoke tests and
are not implied by a green CI badge. No upload has been validated merely by running fake-client tests.

CI checks Python 3.11–3.13 on Linux. Other operating systems have not been validated. A separate quality job runs Ruff lint/format, mypy, Bandit, dependency
auditing, package build/metadata checks, workflow validation and secret scanning. All required jobs
must pass for the exact commit being reviewed. There is no deployment job.

Workflow and secret checks can also run locally on Linux x86_64 with
`bash scripts/check-workflows-and-secrets.sh`. The helper verifies the pinned tool archives against
reviewed SHA-256 hashes. Update both version and hash when upgrading either tool.

Ruff applies to all Python files. Mypy currently checks the config, storage, media, queue, YouTube
editorial policy and CLI boundaries; the inherited analysis and rendering stages are not fully typed. Coverage of
subprocess CLI tests is not attributed to the parent coverage process, so the reported percentage
understates those integration checks. No minimum coverage percentage is enforced.
