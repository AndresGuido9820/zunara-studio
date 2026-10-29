# Security

Supported version: the latest `0.1.x` commit on `main`.

Report vulnerabilities through GitHub's private “Report a vulnerability” flow for this repository.
If that flow is unavailable, open an issue asking for a private contact without including exploit
details, tokens or private files. Ordinary bugs belong in public issues with synthetic fixtures.

The CLI runs with your user permissions. Use trusted workspaces and media. Keep FFmpeg and
`yt-dlp` updated through trusted package sources. Do not run acquisition/render stages as root.
Tokens are private JSON in your config directory. Review the requested OAuth permissions, use
the exact channel ID and revoke access through your Google account when it is no longer needed.

Static checks and dependency audits reduce risk; they do not prove that media decoders, platform
services or all dependencies are vulnerability-free. This project does not bypass platform eligibility,
copyright controls or verification. CI does not have platform credentials and never uploads videos.
