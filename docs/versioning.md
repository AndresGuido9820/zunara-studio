# Versions and releases

Use Semantic Versioning: `MAJOR.MINOR.PATCH`. The source of truth is the version in
`pyproject.toml`; the CLI reads installed package metadata. Payloads and package metadata use
`0.1.0` without a prefix. Annotated Git tags use lowercase `v`, such as `v0.1.0`.

| Change | Version increment |
| --- | --- |
| Remove or rename a public command, field or environment variable; change its type or meaning | Major |
| Add a compatible command, optional field or parameter with a default | Minor |
| Correct a defect or update a dependency without changing the contract | Patch |

During `0.x`, use a minor increment for incompatible development changes and still provide migration
notes. Move to `1.0.0` when the public CLI and queue contract are ready for downstream users to rely on.
Pre-releases use alpha, beta or release-candidate identifiers; version ordering follows SemVer and
Python package metadata follows PEP 440 (for example, `0.2.0rc1`).

## Release procedure

1. Review changes since the last tag and choose the version from their compatibility impact.
2. Update `pyproject.toml` and its lockfile in a `chore(release):` commit through a PR.
3. Merge with a merge commit after checks pass on the final head.
4. Check that the merged commit also passes CI, then create an annotated tag on that commit:

   ```sh
   git tag -a v0.1.0 -m 'Release v0.1.0'
   git push origin v0.1.0
   ```

5. Write a GitHub release in English, grouping changes under Added, Changed, Fixed, Removed and
   Breaking as applicable. Include migration steps and attach built artifacts when appropriate.

Tags and released versions are immutable. A faulty release requires a new version; never move a tag.
GitHub Releases hold the release notes; do not maintain a second changelog in the checkout.
Publishing a package remains a manual operation. There is no CD or automatic video publication.

For a patch to an older supported release, branch `hotfix/X.Y.Z` from its tag, review and tag the fix,
then bring the fix back to `main`. Keep the original tag intact.

References: [SemVer](https://semver.org/), [PEP 440](https://packaging.python.org/en/latest/specifications/version-specifiers/),
[annotated tags](https://git-scm.com/docs/git-tag).
