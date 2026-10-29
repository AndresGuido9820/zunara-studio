# Branches and merges

`main` is the stable branch. This project has no deployment environment or `dev` branch.
Start independent work from freshly fetched `origin/main`:

```sh
git fetch origin
git switch --create fix/queue-validation origin/main
```

Use `feat/`, `fix/`, `docs/`, `refactor/`, `test/`, `ci/` or `build/` followed by a short slug.
Never branch from an unrelated feature by accident. If a change really depends on another PR,
branch explicitly from it, set that PR's branch as the base and document the dependency.

1. Make atomic commits with their tests, following [commit conventions](commits.md).
2. Run local checks and inspect the complete diff, including generated files and the lockfile.
3. Open a draft PR, then fill the [review description](pull-requests.md) and add a type label.
4. Request review once required CI passes on the current head. Address blocking findings and
   rerun the relevant checks after changes. Update the description to match the final implementation.
5. Merge with **Create a merge commit**. Keep thematic commits; squash only local WIP/fixup noise.
   Preserve GitHub's default `Merge pull request #N from …` subject.
6. Delete the merged branch and prune local references.

No direct feature pushes to `main`, no force pushes to shared branches, and no silent history rewrites.
If a release is faulty, revert through a PR rather than moving a published tag. Branch protection
requires the named CI checks. A sole maintainer still uses PRs and records check results in the PR; the project
does not require a second account to bootstrap maintenance.

## Releases

Use SemVer: major breaks compatibility, minor adds a compatible feature, patch fixes a defect.
Version strings contain three numbers; Git tags use lowercase `v`, e.g. `v0.1.0`.
Keep `pyproject.toml` as the version source of truth. Tags are immutable.
Create a GitHub release only from a checked commit. PyPI publication and deployment are manual
future steps; merging a branch publishes neither a package nor a video.

See [versioning and releases](versioning.md) for the release procedure and hotfix branches.
