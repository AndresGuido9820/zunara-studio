## Summary

What this delivers and for whom. State the base branch, thematic commits and merge strategy.
Link dependent PRs when this change is stacked.

## Context and motivation

Describe the problem and link the issue or public design document.

## What changes

Explain the resulting behavior and important decisions. Include valid synthetic examples of changed
CLI, JSON and environment contracts. For changed resources or persisted records, include a small
Mermaid diagram, resource inventory and data types, readers/writers, null semantics, timezones,
constraints and actual integrity guarantees. Verify definitions on this branch; date any live checks.

## Services and resources

Distinguish new, modified, reused, imported and removed resources. Separate this PR from changes in
other PRs and manual actions already performed. State what is not verified or why this is inapplicable.

## Files

Explain the important production, test and documentation changes, with paths.

## Test plan and evidence

Exact commands, numerical results and reproduction steps. Identify untested paths and manual checks.

## Risks, compatibility and rollback

Explain risks, mitigation, contract breaks and migration order. Give concrete rollback steps,
including how to preserve data. Mark a breaking change with its label and migration instructions.

## Out of scope

Adjacent work deliberately deferred, with links when available.

## Reviewer guide

Suggested reading order and the areas requiring particular attention.

- [ ] Tests and quality checks pass on this head.
- [ ] Contracts and resources are documented, or inapplicability is explained.
- [ ] Documentation, compatibility and migration instructions match the final implementation.
- [ ] Manual actions and unverified behavior are stated explicitly.
- [ ] No credentials, personal media or private service data are included.
- [ ] Type/state labels are current and review comments are addressed.
