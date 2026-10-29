# Commit conventions

The history should explain what changed, where and why without requiring someone to reconstruct
a conversation. Use English Conventional Commits:

```text
type(scope): imperative description

Explain why when the reason is not obvious. Wrap the body at 72 columns.

Closes #123
```

Keep subjects within 72 characters, start the description with a lowercase verb, and omit the final
period. Read it as “If applied, this commit will …”. A commit has one intention and must be revertible
without leaving broken tests. Include the tests with the feature/fix; do not separate them just to
make a smaller code commit. Use a body for non-obvious bugs, tradeoffs, contract changes and manual steps.

Types: `feat`, `fix`, `refactor`, `docs`, `test`, `chore`, `ci`, `build`, `perf`, `revert`.
Scopes: `cli`, `queue`, `render`, `analysis`, `youtube`, `security`, `docs`, `ci`.
Omit the scope only for a genuinely repository-wide change. `perf` needs a measurement.

```text
fix(queue): reject changed media after review

An editor can replace a clip without refreshing the queue. Recalculate
its digest before export so an old approval cannot authorize new bytes.
```

Mark contract breaks with both `!` and a migration footer:

```text
feat(youtube)!: require an explicit channel id

BREAKING CHANGE: upload scripts must pass --channel-id. Run the auth
command to find the authorized channel ID before updating the script.
```

`Closes #N` closes an issue; `Refs #N` links without closing it. Clean local WIP/fixup commits before
review, preserving useful thematic commits. Avoid “misc fixes”, phase numbers and automatic attribution
footers. The author is responsible for reviewing and owning the submitted work.

Use `.gitmessage` with `git config --local commit.template .gitmessage` if helpful.
