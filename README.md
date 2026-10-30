# Zunara Studio

[![CI](https://github.com/AndresGuido9820/zunara-studio/actions/workflows/ci.yml/badge.svg)](https://github.com/AndresGuido9820/zunara-studio/actions/workflows/ci.yml)

Turn long streams into short vertical videos. Find candidate moments using chat and audio signals,
render captions, review a local queue, then export clips or upload them to your YouTube channel.

Zunara is a Python command-line tool. It runs on your computer; it has no hosted backend or scheduler.
Publishing is a separate, explicit step. Preview is the default and uploads are private unless requested.

## Install

Requires Python 3.11 or newer and FFmpeg (including `ffprobe`, libass and libx264).
Install FFmpeg with your system package manager, then:

```sh
git clone https://github.com/AndresGuido9820/zunara-studio.git
cd zunara-studio
uv sync --locked --extra youtube
uv run zunara doctor
```

Alternatively, `pip install '.[youtube]'` installs from a checkout. This project is not yet published
on PyPI. Add `--extra analysis` for audio matching, `--extra asr` for speech recognition,
or `--extra downloads` for `yt-dlp`. Speech recognition downloads a model on first use.

## Start with a local clip

Create a workspace outside the checkout:

```sh
mkdir -p ~/Videos/my-clips/out
cp /path/to/your/clip.mp4 ~/Videos/my-clips/out/first-clip.mp4
uv run zunara inspect ~/Videos/my-clips/out/first-clip.mp4
uv run zunara queue ~/Videos/my-clips
```

Edit `~/Videos/my-clips/out/queue.json`: set `title`, `description` and `tags`. Watch the video,
check its captions and source attribution, then approve the specific clip:

```sh
uv run zunara approve ~/Videos/my-clips --only first-clip
uv run zunara export ~/Videos/my-clips --only first-clip --platform tiktok
```

Export creates a video, caption file and hash manifest under your user data directory.
`--destination /path/to/ready` selects another location. It does not post anything. Existing export batches are never overwritten; choose a fresh destination
for a new batch to avoid mixing old and newly selected clips.
Approval is tied to the video bytes; changing the video requires another review. Caption export uses
a conservative 100-character title and 5000-character description limit; adjust the resulting text
to the destination platform before posting.

## YouTube

1. Create a channel and a Google Cloud project. Enable **YouTube Data API v3**.
2. Configure OAuth consent and a **Desktop app** OAuth client. For an app in testing,
   add your Google account as a test user.
3. Download its client JSON outside the repository. Set the path and authorize:

```sh
export ZUNARA_YOUTUBE_CLIENT_SECRET="$HOME/.config/zunara/client_secret.json"
uv run zunara auth --channel my-channel
```

The command prints the authorized channel name and ID. Use that exact ID for every upload:

```sh
uv run zunara publish ~/Videos/my-clips --channel my-channel \
  --channel-id UC_YOUR_CHANNEL_ID --only first-clip
# Review the preview, then explicitly execute:
uv run zunara publish ~/Videos/my-clips --channel my-channel \
  --channel-id UC_YOUR_CHANNEL_ID --only first-clip --privacy private --execute
```

Scheduling uses `--schedule 2030-01-01T12:00:00-05:00 --every-hours 3`. The timezone is required.
Scheduled uploads stay private until their publication time. `uploaded_at` records upload completion;
`scheduled_at` records the requested release time, not proof that the platform published it.

If an upload is interrupted, its state stays `uploading`. Check YouTube Studio before editing the
queue to retry: the server might already have accepted the video. Zunara does not automatically retry
an upload with an unknown outcome.

## Topic policy

You can limit a platform to specific topics. Put the policy in your user config directory, next to
your OAuth files, never in a workspace or this repository:

```json
{"platforms": {"x": {"allowed_topics": ["programming"]}}}
```

Save it as `publishing-policy.json` in the directory `zunara doctor` reports as `config_dir`, then set
a `topic` on each queue entry, for example `"topic": "programming"`. Topic names are lowercase
letters, digits, `_` or `-`. For a configured platform, `zunara export` and `zunara publish` reject
the whole selection if any clip has no topic or a topic that isn't listed. Nothing is exported
and no YouTube client is opened. Platforms not listed, and setups without the file, are unrestricted.
Known platforms are `x`, `youtube`, `tiktok` and `instagram`. A malformed file blocks every
export and publish until it is fixed.

The policy is checked by the CLI. Calling `zunara.queue.export` or `zunara.youtube.upload`
directly from Python does not apply it; call `zunara.editorial.load_policy(...).check(...)` first.

## Find and render moments

The download stage uses Kick's website endpoints and `yt-dlp`; these can change independently of
Zunara. Audio matching requires the `analysis` extra. Rendering requires the `asr` extra and the
**Anton** font (install it locally or expect font fallback).

```sh
uv run zunara fetch example-channel --workspace ~/Videos/work
uv run zunara match /path/to/audio16k.wav /path/to/matches.json /path/to/query.wav
uv run zunara rank /path/to/workspace 10
uv run zunara render /path/to/workspace 60 95 --hook 'An unexpected answer' --name first-clip
```

To cut from a video file you already have, such as a downloaded stream fragment, pass it with
`--source`. The workspace then only receives the output; it needs no `base_url.txt` or
`audio16k.wav`:

```sh
uv run zunara render ~/Videos/work 12.5 41 --source ~/Videos/stream-part.mp4 \
  --hook 'An unexpected answer' --name first-clip
```

Times are seconds into the source file. The source must have video and audio and must cover the
interval. Only the selected audio is transcribed, the source file is left untouched, and the
command refuses to replace an existing `out/<name>.mp4` or `.ass`. Without `--source`, `render`
keeps cutting from the workspace VOD as before.

The renderer uses VAAPI when available, otherwise software H.264. It keeps a wide foreground with
a blurred background so people at the edge of a stream are not lost to a tight vertical crop.
Ranking proposes moments; it does not judge whether a clip is accurate or worth publishing.

## Platform support

| Platform | Current support |
| --- | --- |
| YouTube | OAuth, channel verification, upload preview, private/public/unlisted uploads and scheduling |
| TikTok | Local video and caption export; no direct posting integration |
| Instagram | Local video and caption export; no direct posting integration |
| X | Local video and caption export; no direct posting integration |

API access, account eligibility and platform limits are separate from installing Zunara.
Use media you own or have permission to publish. An exported clip is not a copyright clearance.

## Configuration and safety

Paths follow your operating system's user config/data directories. Override them with
`ZUNARA_CONFIG_DIR`, `ZUNARA_DATA_DIR` and `ZUNARA_YOUTUBE_CLIENT_SECRET`.
The CLI reads environment variables; it does not load `.env` automatically.
OAuth tokens are stored as private JSON, never pickle. Do not commit tokens, client secrets,
signed stream URLs, downloaded chat or media.

Workspaces have one writer at a time. Do not edit media or queue metadata during export/upload.
See [architecture](docs/architecture.md), [testing](docs/testing.md),
[Git workflow](docs/git-workflow.md), [migration](docs/migration.md) and [security](SECURITY.md).

## Development

```sh
uv sync --locked --extra youtube --group dev
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run bandit -c pyproject.toml -r src
uv run pytest
uv build
uv run twine check dist/*
bash scripts/check-workflows-and-secrets.sh
```

Contributions follow [CONTRIBUTING.md](CONTRIBUTING.md). Commits use Conventional Commits in English;
scopes are `cli`, `queue`, `render`, `analysis`, `youtube`, `security`, `docs` and `ci`.
Branches and PRs follow the linked Git workflow; releases follow [versioning](docs/versioning.md).
Licensed under [MIT](LICENSE).
