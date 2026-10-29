# Pull requests and review

A reviewer should understand the problem, resulting behavior and evidence without knowing the
conversation that produced the patch. Titles are concrete, imperative and stand alone. Avoid phase
numbers, branch names and “improvements”. Use one language consistently; code and commits use English.

## Two description sizes

For docs, tests, styling or a refactor with no behavior change: **What changes, Why, Evidence,
Risk and rollback**. Dependency updates qualify when behavior and contracts remain unchanged.
A short description still needs actual evidence.

For behavior, CLI/JSON/environment contracts, resources or migrations, use the complete template:
**Summary; Context and motivation; What changes; Services and resources; Files; Test plan and
evidence; Risks, compatibility and rollback; Out of scope; Reviewer guide**. Omit a genuinely
inapplicable section with a short reason, preserving the order. Add a short final checklist.
Copy the [full template](../.github/PULL_REQUEST_TEMPLATE/full.md) for these changes.
When in doubt, use the complete format.

For data/resource changes, include a small Mermaid diagram, resource inventory and record examples.
Say what is new, modified or reused; show identifiers, types, who writes/reads, null semantics,
timezones, integrity guarantees, migration order and rollback. Separate code changes from any manual
action already performed. Mark live platform behavior “not verified” when only a fake-client test ran.
Never copy private account names, keys or signed URLs into examples.

Aim below 400 reviewable lines, excluding lockfiles and unchanged moves. Split changes above 1000
lines unless the reviewer explicitly accepts a coherent bootstrap or mechanical migration.

## Before requesting review

Read your own full diff before requesting review. State exact commands/results, unresolved failures
and manual steps. Do not tick a checklist item without evidence. Comments use `issue (blocking)`,
`suggestion`, `question`, `nit (non-blocking)`, `thought` or `praise` so intent is clear.
Keep review discussion in the PR comments.

Type labels: `enhancement`, `bug`, `documentation`, `refactor`, `infra`, `dependencies`.
State labels: `needs-review`, `blocked`, `breaking-change`. Use one type label unless the scope
justifies more; inherit it from the issue when closing one. Keep state labels current, explain
a blocker and remove `needs-review` after approval.

The maintainer checks scope, behavior, security, tests, documentation and rollback before a merge.
Review applies to the exact head SHA; a later code change invalidates earlier validation evidence.

References: [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/),
[Conventional Comments](https://conventionalcomments.org/),
[Google's CL descriptions](https://google.github.io/eng-practices/review/developer/cl-descriptions.html).
