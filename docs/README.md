# Documentation

Zunara is a local command-line tool. Start with a video file; add stream analysis or YouTube access
when you need them. Shell examples use POSIX syntax and run from the checkout unless stated.

## Start here

1. [Install Zunara](guides/installation.md) and check its dependencies.
2. [Make your first clip](guides/first-clip.md), review it and export without an account.
3. [Use Claude Code or Codex](guides/agents.md) to operate the same CLI through an agent.

## Task guides

- [Rendering and stream analysis](guides/rendering.md): timestamps, layouts, captions and ranking.
- [Multi-platform API publishing](guides/social-api.md): account connections, upload and receipts.
- [YouTube](guides/youtube.md): authorization, online preview, uploads, schedules and recovery.
- [Troubleshooting](guides/troubleshooting.md): symptoms, checks and fixes.

## Reference and design

- [Commands](reference/cli.md): complete arguments, defaults, results and side effects.
- [Workspace and configuration](reference/workspace.md): files, fields, approval and topic policy.
- [Architecture](architecture.md) and [ADR 001](adr/001-local-first-cli.md): boundaries and decisions.
- [Testing](testing.md): test layers, CI and the limits of automated verification.

## Maintaining the project

[Contributing](../CONTRIBUTING.md) · [Git workflow](git-workflow.md) · [Commits](commits.md) · [Pull
requests](pull-requests.md) · [Releases](versioning.md) · [Migration](migration.md) ·
[Security](../SECURITY.md)

## Where it adds value

Zunara joins work that otherwise needs separate scripts: candidate ranking, caption rendering,
metadata review, approval tied to media bytes, batch export and a channel-checked YouTube upload.
The same commands work for a person or an agent, and the workspace keeps the state between runs.

For a single cut without captions or a review queue, FFmpeg alone may be simpler. Ranking needs VOD
signals; a bare local MP4 is enough for rendering, but not for the ranking pipeline. Zunara proposes
moments rather than judging context, rights, factual accuracy or audience appeal.

There is no graphical editor, hosted backend, bundled MCP server or daemon. Agents call the CLI; no
Claude or OpenAI SDK is required by Zunara. Caption translation and screen recording are separate
editing tasks. TikTok, Instagram and X support local exports and the optional Upload-Post API.
YouTube also has a direct publisher using your own Google project.
